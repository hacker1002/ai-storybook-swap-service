"""Pure resolver helpers for the remix sprite-swap enqueue + handler.

Shared between the router (precheck → HTTP codes) and the handler (execution →
result errors) — DRY. All functions are PURE (accept dicts from DB, return
dataclasses/lists) — NO DB I/O, NO logging (logging is the caller's concern) —
so they are trivially unit-testable and never block the event loop.

A sprite entry (`remixes.sprites[]`, identified by `id` uuid) is a cluster of
crop sheets. Each cell is SINGLE-SUBJECT — `{type, object_key, variant_key}`
(NOT the multi-subject `tags[]` of `mixes[]`). The swap lineup is the distinct
set of `crops[].object_key` (type='character') across every crop sheet — the
lineup objects are CONSTANT across sheets, so the object pool is resolved ONCE
(`resolve_sprite_object_map`); each sheet then projects the subset of objects
ACTUALLY present in its cells (`select_sheet_objects`).

Per-object per-trait model (mirrors job 05 mix-swap structurally but):
  - swap source = human GỐC (`humans.visual_profiles[].converted_image`) + the
    enabled per-trait descriptions — NOT a pre-swapped `visual_swap_url`;
  - object pool intersect is by `object_key` only (no variant in the join — one
    human + trait-set applies to every cell of that object_key);
  - the winner mutex (`clear_foreign_finals`) keys on
    `(type, object_key, variant_key)` cross-sheet.

`buildSwapObject` (→ `build_swap_object`) returns a `SpriteSwapObject`-shaped
dict per the primitive's schema (`swap_sprite_sheet.py`). When the enabled
traits resolve to ZERO descriptions (every enabled trait missing a description
in the human profile) the function returns None → the caller folds the
object_key into `missing` → `MISSING_OBJECT_CONFIG` (hướng 1, defense-in-depth).

PII discipline: never log/echo URLs, human descriptions, or trait descriptions.
Functions only return structured data handed to the AI primitive; error info
carries `object_key` (non-PII) only.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

from src.models.requests.trait_types import TRAIT_TYPES  # canonical ordering

__all__ = [
    "SpriteObjectMapResult",
    "sprite_lineup_object_keys",
    "find_sprite_by_id",
    "sheet_object_keys",
    "select_sheet_objects",
    "build_swap_object",
    "clear_foreign_finals",
    "resolve_sprite_object_map",
]


@dataclasses.dataclass(slots=True, frozen=True)
class SpriteObjectMapResult:
    """Resolved sprite object pool — constant across every sheet of the sprite.

    - `object_map`: object_key → `SpriteSwapObject`-shaped dict (the same objects
      handed to `run_swap_sprite_sheet`, keyed for O(1) per-sheet projection via
      `select_sheet_objects`).
    - `lineup`: distinct character object_keys derived from the sprite's crops
      (sorted, deterministic).
    - `missing`: object_keys that COULD NOT be fully resolved (incomplete
      `remix_config` OR enabled-trait descriptions all empty). FATAL for the
      caller → `MISSING_OBJECT_CONFIG` (hướng 1, defense-in-depth).
    - `object_count`: len(object_map) — informational (constant across sheets).
    """

    object_map: dict[str, dict]
    lineup: list[str]
    missing: list[str]
    object_count: int


# ─── lineup / sheet helpers ─────────────────────────────────────────────────


def find_sprite_by_id(sprites: list | None, sprite_id: str) -> dict | None:
    """Find the `remixes.sprites[]` entry whose `id == sprite_id`."""
    for s in sprites or []:
        if isinstance(s, dict) and s.get("id") == sprite_id:
            return s
    return None


def _crop_object_key(crop: Any) -> Optional[str]:
    """Extract a character cell's `object_key` (None for non-character / blank)."""
    if not isinstance(crop, dict):
        return None
    if crop.get("type") != "character":
        return None
    object_key = (crop.get("object_key") or "").strip()
    return object_key or None


def sheet_object_keys(sheet: dict) -> list[str]:
    """Distinct character `object_key`s present in ONE sheet's cells, SORTED.

    Single-subject join: one cell carries exactly one `(type, object_key,
    variant_key)`. The per-sheet membership used to project the sprite-wide
    object pool down to the objects ACTUALLY present in the sheet.
    """
    if not isinstance(sheet, dict):
        return []
    seen: set[str] = set()
    for crop in sheet.get("original_crops") or []:
        ok = _crop_object_key(crop)
        if ok:
            seen.add(ok)
    return sorted(seen)


def sprite_lineup_object_keys(sprite: dict) -> list[str]:
    """Distinct character `object_key`s across EVERY crop sheet of the sprite.

    Union of each sheet's `sheet_object_keys`, SORTED for determinism (the router
    + handler resolve independently and must agree). This is the lineup resolved
    once into the object pool; per-sheet subsets come from `select_sheet_objects`.
    """
    seen: set[str] = set()
    for sheet in sprite.get("crop_sheets") or []:
        seen.update(sheet_object_keys(sheet))
    return sorted(seen)


def select_sheet_objects(object_map: dict[str, dict], sheet: dict) -> list[dict]:
    """Project the sprite-wide object pool onto ONE sheet.

    Returns the swap_object dicts whose `object_key` appears in THIS sheet's
    character cells, in sorted-object_key order (deterministic). Object keys
    present in the sheet but absent from `object_map` are simply omitted — the
    object pool is resolved over the union of the lineup, so a fully-resolved
    pool covers every cell (a miss means `resolve_sprite_object_map` already
    flagged it in `missing` → the caller fails the whole job).
    """
    return [object_map[ok] for ok in sheet_object_keys(sheet) if ok in object_map]


def eligible_sheet_crops(sheet: dict) -> list[dict[str, Any]]:
    """Single source of truth for which stored crops are processable this sheet.

    A crop is eligible iff it has a dict `geometry` AND an http `media_url`. The
    SAME filtered+ordered list MUST feed every sheet consumer so they cannot
    diverge on cell membership/order:
      - sprite-swap composer (`_build_core_request`) bakes the sheet from this
        list by index, and its cut helper (`_build_cut_crops`) slices the swapped
        sheet by the same index — a divergence would cut a cell the swap never
        processed and clobber the real winner;
      - detect-defects (`build_detect_request`) feeds this list as `crops[]`, the
        ORIGINAL-sheet reference the core re-composes from when
        `original_sheet_url` is omitted — a divergence would compose a different
        reference than swap used and mislocate defects.
    Returns the rich `SpriteSheetCrop` shape; callers slim it as needed.
    """
    out: list[dict[str, Any]] = []
    for c in sheet.get("original_crops") or []:
        if not isinstance(c, dict) or not isinstance(c.get("geometry"), dict):
            continue
        media_url = c.get("media_url")
        if not isinstance(media_url, str) or not media_url.startswith("http"):
            continue
        g = c["geometry"]
        out.append(
            {
                "type": c.get("type") or "character",
                "object_key": c.get("object_key") or "",
                "variant_key": c.get("variant_key") or "base",
                "media_url": media_url,
                "geometry": {
                    "x": int(g.get("x", 0)),
                    "y": int(g.get("y", 0)),
                    "w": int(g.get("w", 0)),
                    "h": int(g.get("h", 0)),
                },
                "annotation": c.get("annotation"),
            }
        )
    return out


# ─── config / human resolution ──────────────────────────────────────────────


def _find_by_key(entries: list | None, key: str) -> dict | None:
    """Find an entry by `key` in a `remix_config.characters[]` /
    `snapshot.characters[]` list (both are key-discriminated arrays)."""
    for e in entries or []:
        if isinstance(e, dict) and e.get("key") == key:
            return e
    return None


def _enabled_trait_types(cfg: dict) -> list[str]:
    """Enabled trait `type`s of a `remix_config.characters[]` entry (canonical
    order). A missing `traits[]` entry defaults to enabled per DB reader rule,
    but the writer always emits all 5 — here we only count entries present AND
    `is_enabled` truthy, in TRAIT_TYPES order for determinism."""
    enabled: set[str] = set()
    for t in cfg.get("traits") or []:
        if isinstance(t, dict) and t.get("is_enabled") and t.get("type"):
            enabled.add(t["type"])
    return [tt for tt in TRAIT_TYPES if tt in enabled]


def _human_profile(
    humans_by_id: dict[str, dict], human_id: str | None, visual: str | None
) -> dict | None:
    """Resolve `humans[human_id].visual_profiles[name == visual]`.

    `humans_by_id` is a pre-built id → human-row map (caller does the single DB
    read). `visual` = the VisualProfile.name composite-key part. Returns None
    when the human row or the named profile is absent."""
    if not human_id or not visual:
        return None
    human = humans_by_id.get(human_id)
    if not isinstance(human, dict):
        return None
    for vp in human.get("visual_profiles") or []:
        if isinstance(vp, dict) and vp.get("name") == visual:
            return vp
    return None


def build_swap_object(
    object_key: str,
    cfg: dict,
    human_profile: dict,
    snap_char: dict | None,
) -> Optional[dict]:
    """Build a `SpriteSwapObject`-shaped dict for one lineup object.

    - `human_image_url` = the human profile's `converted_image` (appearance src).
    - `human_description` = profile `description` (or '' — model requires
      min_length 1, so callers must NOT pass a profile lacking it; the resolver
      treats an empty converted_image as unresolvable upstream).
    - `swap_traits[]` = the ENABLED traits mapped to the human profile's per-trait
      description, DROPPING any trait whose description is blank (parity 03-old
      Q4). When the result is EMPTY → returns None (the caller folds object_key
      into `missing` → MISSING_OBJECT_CONFIG, hướng 1).
    - `object_context` = slim grounding from the snapshot character.

    PURE — no I/O. Returns None when no enabled trait has a resolvable
    description; the object cannot be meaningfully swapped.
    """
    human_traits = {
        t.get("type"): (t.get("description") or "")
        for t in (human_profile.get("traits") or [])
        if isinstance(t, dict) and t.get("type")
    }
    swap_traits: list[dict] = []
    for trait_type in _enabled_trait_types(cfg):
        desc = (human_traits.get(trait_type) or "").strip()
        if not desc:
            continue
        swap_traits.append({"type": trait_type, "description": desc})

    if not swap_traits:
        return None

    snap = snap_char or {}
    return {
        "object_key": object_key,
        "human_image_url": human_profile.get("converted_image") or "",
        "human_description": (human_profile.get("description") or "").strip(),
        "swap_traits": swap_traits,
        "object_context": {
            "name": snap.get("name") or object_key,
            "basic_info": snap.get("basic_info") or {},
            "personality": snap.get("personality") or {},
            "appearance": snap.get("appearance") or {},
            "visual_description": snap.get("visual_description") or "",
        },
    }


def resolve_sprite_object_map(
    sprite: dict,
    remix_config: dict | None,
    humans_by_id: dict[str, dict],
    snapshot_characters: list | None,
) -> SpriteObjectMapResult:
    """Resolve the sprite-wide object pool ONCE (lineup constant across sheets).

    Walks `sprite_lineup_object_keys(sprite)` (distinct character object_keys,
    sorted → deterministic):
      - cfg has `human_id` + `visual` + the human profile has `converted_image`
        + ≥1 enabled trait → `build_swap_object`. If that returns None (every
        enabled trait's description blank), or any precondition is missing →
        fold object_key into `missing` (FATAL for the caller → hướng 1
        MISSING_OBJECT_CONFIG; does not happen on the normal flow because the
        layout only packs fully-configured chars).

    Errors are RETURNED (not raised) so the caller decides severity:
    empty lineup → NO_SWAP_OBJECTS; non-empty `missing` → MISSING_OBJECT_CONFIG.
    PURE — no I/O, no logging.
    """
    rc = remix_config or {}
    rc_characters = rc.get("characters") or []

    object_map: dict[str, dict] = {}
    missing: list[str] = []
    lineup = sprite_lineup_object_keys(sprite)

    for object_key in lineup:
        cfg = _find_by_key(rc_characters, object_key)
        if not isinstance(cfg, dict):
            missing.append(object_key)
            continue

        human_id = cfg.get("human_id")
        visual = cfg.get("visual")
        profile = _human_profile(humans_by_id, human_id, visual)
        # Preconditions: human_id + visual + resolvable profile w/ converted_image
        # + ≥1 enabled trait. Any miss → object cannot be swapped → `missing`.
        if (
            not human_id
            or not visual
            or not isinstance(profile, dict)
            or not (profile.get("converted_image") or "").strip()
            or not _enabled_trait_types(cfg)
        ):
            missing.append(object_key)
            continue

        snap_char = _find_by_key(snapshot_characters, object_key)
        obj = build_swap_object(object_key, cfg, profile, snap_char)
        if obj is None:
            # Enabled traits but human profile has no usable descriptions.
            missing.append(object_key)
            continue
        object_map[object_key] = obj

    return SpriteObjectMapResult(
        object_map=object_map,
        lineup=lineup,
        missing=missing,
        object_count=len(object_map),
    )


# ─── R1 winner mutex ────────────────────────────────────────────────────────


def clear_foreign_finals(
    sprite: dict,
    key: tuple[str, str, str],
    *,
    except_sheet: int,
) -> int:
    """R1 cross-sheet `is_final` mutex for ONE just-swapped key.

    For every OTHER crop sheet of `sprite` (index != `except_sheet`), walk its
    `swap_results[].crops[]` and clear `is_final` on any crop whose
    `(type, object_key, variant_key)` matches `key`. Pure in-memory mutation
    (persisted with the full-blob `UPDATE remixes` step). Idempotent — re-swap of
    the same sheet re-fires safely. Returns the number of cleared crops.

    Guarantees the invariant: at most one `is_final=true` crop per
    `(type, object_key, variant_key)` across the whole sprite.
    """
    cleared = 0
    crop_sheets = sprite.get("crop_sheets") or []
    for s_idx, sheet in enumerate(crop_sheets):
        if s_idx == except_sheet or not isinstance(sheet, dict):
            continue
        for result in sheet.get("swap_results") or []:
            if not isinstance(result, dict):
                continue
            for crop in result.get("crops") or []:
                if not isinstance(crop, dict):
                    continue
                crop_key = (
                    crop.get("type"),
                    crop.get("object_key"),
                    crop.get("variant_key"),
                )
                if crop_key == key and crop.get("is_final"):
                    crop["is_final"] = False
                    cleared += 1
    return cleared
