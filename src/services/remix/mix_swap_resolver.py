"""Pure resolver helpers for the remix mix-swap enqueue + handler.

Shared between the router (precheck → HTTP codes) and the handler (execution →
result errors) — DRY. Shared roster/visual helpers live in
`swap_roster_resolver.py`. All functions are
PURE (accept dicts from DB, return dataclasses/lists) — NO DB I/O, NO logging
(logging is the caller's concern), so they are trivially unit-testable and never
block the event loop.

A batch entry (`remixes.mixes[]`) is a cluster of crop sheets, identified by
`id` (uuid). Its swap lineup is DERIVED at runtime from the union of every
`crop_sheets[].original_crops[].tags[]` (object_key + variant_key) — there is
no stored `keys[]` lineup anymore (DB-CHANGELOG 2026-05-26 reshape; ⚡lean
rename `crops[]` → `original_crops[]` 2026-06-12, HARD cutover — no fallback
read of the legacy key; stale batches must be re-built via endpoint 01). The
job swaps the WHOLE derived lineup of one batch per sheet via the multi-target
AI primitive `run_swap_mix_sheet`. Because the lineup is constant across every
sheet, `swap_targets[]` is resolved ONCE (here) and reused for all sheets.

⚡rev8 (2026-06-11): `unchanged_references` left the primitive contract (04
rev6) — tokens without a resolvable reference (prop/dangling/disabled) are now
simply SKIPPED; objects outside the target list stay untouched by default.

Token grammar (derived from `original_crops[].tags[]`):
  - token = `${tag.object_key}/${tag.variant_key}` (variant_key null → 'base').
  - `object_key` is a soft ref to `remix.characters[].key` ∪ `remix.props[].key`.
  - `batch_lineup_tokens()` dedups + sorts so the router + handler (which resolve
    independently) derive the SAME lineup.

PII discipline: never log/echo URLs or names. Functions only return structured
data handed to the AI primitive (the primitive does not log URLs either).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from src.services.remix.sprite_finals_resolver import resolve_sprite_finals
from src.services.remix.swap_roster_resolver import (
    coerce_age,
    original_visual_url,
)

__all__ = [
    "MixSwapContext",
    "parse_token",
    "sheet_lineup_tokens",
    "batch_lineup_tokens",
    "find_batch_by_id",
    "select_sheet_targets",
    "build_swap_target",
    "resolve_mix_swap_context",
]


@dataclasses.dataclass(slots=True, frozen=True)
class MixSwapContext:
    """Resolved mix-swap context — constant across every sheet of the mix.

    - `swap_targets`: each element matches the `SwapTarget` shape
      (`key`, `reference_image_url`, `target_base_image_url`, `object_context`).
    - `missing_char_refs`: char tokens that are ENABLED in the remix roster but
      lack a resolvable sprite final (no `sprites[].crop_sheets[].swap_results
      [is_selected].crops[is_final]` for the cell — haven't run the sprite-swap
      job) → FATAL for the caller.
    - `missing_target_base`: swap-target tokens whose `target_base` (original
      visual) could not be resolved. The target is STILL in `swap_targets`; the
      caller decides fatality (FATAL only when ≥2 targets — N=1 needs no
      locator). Validation S1.
    - `target_map`: token → swap_target dict (the SAME objects as in
      `swap_targets`, keyed for O(1) per-sheet projection). A sheet's targets are
      the subset of this map whose token appears in THAT sheet's `original_crops[].tags[]`
      (see `select_sheet_targets`) — the batch lineup is the union, but each sheet
      only swaps the objects actually present in it.
    - `target_count`: number of resolved swap targets.
    """

    swap_targets: list[dict]
    missing_char_refs: list[str]
    missing_target_base: list[str]
    target_count: int
    target_map: dict[str, dict]


# ─── token / batch helpers ──────────────────────────────────────────────────


def parse_token(token: str) -> tuple[str, str]:
    """Split a lineup token into `(object_key, variant_key)`.

    `${objectKey}/${variantKey}` → split on the FIRST `/` (variant_key may
    itself contain `/`). Bare `${objectKey}` → variant_key defaults to `'base'`.
    Both parts are stripped; an empty variant part falls back to `'base'`.
    """
    raw = (token or "").strip()
    if "/" in raw:
        okey, vkey = raw.split("/", 1)
        okey = okey.strip()
        vkey = vkey.strip() or "base"
        return okey, vkey
    return raw, "base"


def sheet_lineup_tokens(sheet: dict) -> list[str]:
    """Derive the swap lineup of ONE crop sheet from its own crop tags.

    Distinct `${object_key}/${variant_key}` tokens from
    `sheet.original_crops[].tags[]` (⚡lean rename 2026-06-12 — hard cutover,
    no `crops` fallback), SORTED for determinism. A tag without `object_key` is
    skipped; `variant_key` null/empty → 'base'. This is the per-sheet
    membership used to project the batch-wide target pool down to the objects
    ACTUALLY present in the sheet (a sheet swaps only what it contains, not the
    whole batch lineup).
    """
    seen: set[str] = set()
    if not isinstance(sheet, dict):
        return []
    for crop in sheet.get("original_crops") or []:
        if not isinstance(crop, dict):
            continue
        for tag in crop.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            object_key = (tag.get("object_key") or "").strip()
            if not object_key:
                continue
            variant_key = (tag.get("variant_key") or "base").strip() or "base"
            seen.add(f"{object_key}/{variant_key}")
    return sorted(seen)


def batch_lineup_tokens(batch: dict) -> list[str]:
    """Derive the swap lineup of a batch from its aggregate crop tags.

    Union of every sheet's `sheet_lineup_tokens` → distinct tokens, SORTED for
    determinism (the router + handler resolve independently and must agree). This
    is the union used to RESOLVE the full target pool once; the actual per-sheet
    target subset is computed by `select_sheet_targets`.
    """
    seen: set[str] = set()
    for sheet in batch.get("crop_sheets") or []:
        seen.update(sheet_lineup_tokens(sheet))
    return sorted(seen)


def find_batch_by_id(mixes: list | None, batch_id: str) -> dict | None:
    """Find the `remixes.mixes[]` entry whose `id == batch_id`."""
    for m in mixes or []:
        if isinstance(m, dict) and m.get("id") == batch_id:
            return m
    return None


def select_sheet_targets(target_map: dict[str, dict], sheet: dict) -> list[dict]:
    """Project the batch-wide target pool onto ONE sheet.

    Returns the swap_target dicts whose token appears in THIS sheet's
    `original_crops[].tags[]`, in sorted-token order (deterministic). ⚡rev6: this order
    IS the variant-sheet cell order — target i fills cell i+1 on both the old
    and the new variant sheet, so the caller MUST NOT re-sort. Tokens present in
    the sheet but absent from `target_map` (prop/dangling/disabled — no
    resolvable reference) are simply omitted: objects outside the target list
    stay untouched by default (no unchanged_references since 04 rev6).
    """
    return [
        target_map[tok] for tok in sheet_lineup_tokens(sheet) if tok in target_map
    ]


# ─── lineup → swap_targets / unchanged resolution ───────────────────────────


def _find_entity(entities: list | None, object_key: str) -> dict | None:
    """Find an entry by `key` in a `remix.characters[]` / `props[]` /
    `snapshot.characters[]` / `props[]` list."""
    for e in entities or []:
        if isinstance(e, dict) and e.get("key") == object_key:
            return e
    return None


def _variant_in(entity: dict | None, variant_key: str) -> dict | None:
    """Find `entity.variants[key == variant_key]` (None key normalizes to
    'base')."""
    for v in (entity or {}).get("variants") or []:
        if not isinstance(v, dict):
            continue
        vkey = v.get("key")
        if ("base" if vkey is None else vkey) == variant_key:
            return v
    return None


def build_swap_target(
    token: str,
    snap_entity: dict | None,
    variant_key: str,
    ref_url: str,
    is_char: bool,
) -> dict:
    """Build a `SwapTarget`-shaped dict for one lineup token.

    `key` = the variant-qualified token (unique within the request).
    `reference_image_url` = the resolved sprite final media_url (the NEW
    identity) — read from `sprites[].crop_sheets[].swap_results[is_selected]
    .crops[is_final]` by the caller, not the removed `visual_swap_url` bridge.
    `target_base_image_url` = the original (pre-swap) visual = locator.
    `object_context` = slim grounding; `age` is null for non-character objects.
    """
    snap_variant = _variant_in(snap_entity, variant_key) or {}
    object_context = {
        "name": (snap_entity or {}).get("name") or "",
        "age": coerce_age((snap_entity or {}).get("basic_info")) if is_char else None,
        "appearance": snap_variant.get("appearance") or {},
        "visual_description": snap_variant.get("visual_description") or "",
    }
    return {
        "key": token,
        "reference_image_url": ref_url,
        "target_base_image_url": original_visual_url(snap_entity, variant_key),
        "object_context": object_context,
    }


def resolve_mix_swap_context(
    batch: dict,
    remix_characters: list | None,
    remix_props: list | None,
    snapshot_characters: list | None,
    snapshot_props: list | None,
    remix_sprites: list | None = None,
) -> MixSwapContext:
    """Resolve `swap_targets[]` for one batch entry.

    `remix_sprites` (= `remixes.sprites[]`) is resolved ONCE via
    `resolve_sprite_finals` into a `cellKey -> media_url` map; that map is the
    SOURCE of every target's `reference_image_url` (the removed `visual_swap_url`
    bridge). `cellKey = ${type}/${object_key}/${variant_key}` matches the
    mix-token grammar (variant null→'base' on both sides).

    Walks `batch_lineup_tokens(batch)` (derived from `original_crops[].tags[]`, sorted →
    deterministic):
      - char/prop token WITH a resolvable sprite final → swap_target. If its
        `target_base` (original visual) is unresolvable, the target is STILL
        included (base is optional for N=1) and the token is recorded in
        `missing_target_base` so the CALLER decides fatality by lineup size
        (locator only needed when ≥2 targets — Validation S1).
      - ENABLED char token (in remix roster) WITHOUT a sprite final →
        `missing_char_refs` (hasn't run the sprite-swap job — FATAL for caller).
      - prop / dangling / disabled token → SKIPPED (⚡rev8: no
        unchanged_references — objects outside the target list stay untouched
        by default).

    Errors are RETURNED (not raised) so the caller (router → HTTP / handler →
    result-error) decides the severity. PURE — no I/O, no logging.
    """
    # Sprite finals → cellKey -> media_url (the reference source; replaces the
    # removed per-variant `visual_swap_url` bridge). Resolved ONCE per batch.
    finals_map = resolve_sprite_finals(remix_sprites)

    swap_targets: list[dict] = []
    target_map: dict[str, dict] = {}
    missing_char_refs: list[str] = []
    missing_target_base: list[str] = []

    for token in batch_lineup_tokens(batch):
        object_key, variant_key = parse_token(token)

        remix_char = _find_entity(remix_characters, object_key)
        is_char = remix_char is not None

        # Reference = the sprite final for this cell. cellKey carries the object
        # TYPE (character/prop) — derived from roster membership (a key present
        # in remix.characters[] is a character; otherwise a prop).
        type_str = "character" if is_char else "prop"
        ref_url = finals_map.get(f"{type_str}/{object_key}/{variant_key}")

        snap_entity = _find_entity(snapshot_characters, object_key) or _find_entity(
            snapshot_props, object_key
        )

        if ref_url:
            target = build_swap_target(
                token, snap_entity, variant_key, ref_url, is_char
            )
            # Base optional (N=1); record miss for the caller's N-aware decision,
            # but KEEP the target — do not drop (Validation S1).
            if not target["target_base_image_url"]:
                missing_target_base.append(token)
            swap_targets.append(target)
            target_map[token] = target
        elif is_char:
            # Enabled char (present in remix roster) but no sprite final yet
            # → hasn't run the sprite-swap job → FATAL (half-applied swap).
            missing_char_refs.append(token)
        # else: prop / dangling / disabled → skipped (⚡rev8 — untouched by default).

    return MixSwapContext(
        swap_targets=swap_targets,
        missing_char_refs=missing_char_refs,
        missing_target_base=missing_target_base,
        target_count=len(swap_targets),
        target_map=target_map,
    )
