"""Shared swap-roster resolver (used by `remix_mix_swap`).

Pure helpers for resolving the original (pre-swap) visuals + the roster diff of
co-present characters that must stay UNCHANGED during a swap. Previously colocated
with the now-removed character-swap (job 04) resolver; relocated here under a
neutral name so the mix-swap (job 05) router + handler depend on a domain module
rather than a dead sibling.

All functions are PURE (accept dicts from DB, return tuples/lists) — NO DB I/O,
NO logging (logging is the caller's concern) — so they are trivially unit-testable
and never block the event loop.

PII discipline: never log/echo URLs or names. Functions only return structured
data handed downstream; logging is the caller's concern.

Multi-character disambiguation (roster-based) — per job two AI-primitive aux
inputs are resolved server-side:
  - target_base_image_url  = the TARGET object's ORIGINAL (pre-swap) visual for
                             the sheet's variant (locates which figure to swap)
  - unchanged_references[] = ORIGINAL (base-variant) visuals of co-present
                             characters that must NOT be swapped

A swap lineup can include crops cut from SHARED illustration layers (the swapped
object drawn together with a co-present char). Such co-present chars are absent
from `remix.characters[]` but present in `snapshot.characters[]`. Hence the set
that must stay unchanged = snapshot keys − remix keys − {lineup object keys}. We
resolve this from the ROSTER diff (not per-layer tags) because stored crops do
NOT carry the `spread_id`/`layer_id` linkage a layer-scoped resolve would need.
"""

from __future__ import annotations

__all__ = [
    "coerce_age",
    "effective_illustration_url",
    "original_visual_url",
    "resolve_unchanged_from_roster_multi",
]


def coerce_age(basic_info: dict | None) -> str | None:
    """Stringify `basic_info.age` for the slim character/object context (model
    expects a string). Returns None when absent/blank. Reused by the mix-swap
    resolver to build `object_context.age`."""
    age = (basic_info or {}).get("age")
    if age is None:
        return None
    age_str = str(age).strip()
    return age_str or None


def effective_illustration_url(variant: dict | None) -> str | None:
    """Resolve a variant's effective visual URL (canonical chain).

    `final_hires_media_url → illustrations[].find(is_selected).media_url →
    illustrations[0].media_url`. Character/prop variants have no sketch-level
    `media_url`, so that last layer-only fallback is intentionally omitted.
    Returns None when nothing resolvable.
    """
    if not isinstance(variant, dict):
        return None
    hires = variant.get("final_hires_media_url")
    if isinstance(hires, str) and hires:
        return hires
    illustrations = variant.get("illustrations") or []
    selected = next(
        (
            i
            for i in illustrations
            if isinstance(i, dict) and i.get("is_selected") and i.get("media_url")
        ),
        None,
    )
    if selected:
        return selected["media_url"]
    for i in illustrations:
        if isinstance(i, dict) and isinstance(i.get("media_url"), str) and i["media_url"]:
            return i["media_url"]
    return None


def original_visual_url(snap_char: dict | None, variant_key: str | None) -> str | None:
    """Original (pre-swap) visual URL of a snapshot character for a variant.

    Variant resolution: `variants[key==(variant_key or 'base')]` →
    `variants[type==0]` → `variants[0]`. Returns None when the character has no
    variants or no resolvable illustration URL.
    """
    variants = (snap_char or {}).get("variants") or []
    if not variants:
        return None
    target_key = "base" if variant_key is None else variant_key
    chosen = next(
        (v for v in variants if isinstance(v, dict) and v.get("key") == target_key),
        None,
    )
    if chosen is None:
        chosen = next(
            (v for v in variants if isinstance(v, dict) and v.get("type") == 0), None
        )
    if chosen is None:
        chosen = variants[0] if isinstance(variants[0], dict) else None
    return effective_illustration_url(chosen)


def resolve_unchanged_from_roster_multi(
    snapshot_characters: list | None,
    remix_characters: list | None,
    exclude_keys: set[str],
    cap: int,
) -> list[dict]:
    """Resolve unchanged-character refs from the snapshot/remix roster diff,
    excluding a SET of object keys.

    The set that must NOT be swapped in this job = characters present in the
    snapshot but absent from `remix.characters[]` (disabled co-present chars),
    minus every key in `exclude_keys` (e.g. all the mix lineup's object keys —
    those are already handled as swap_targets or folded explicitly). For each
    remaining character resolve the BASE-variant original visual (the identity
    anchor handed to Gemini), keeping snapshot order, skipping any with no
    resolvable visual, capped at `cap`. PURE — no I/O.
    """
    remix_keys = {
        ch.get("key")
        for ch in (remix_characters or [])
        if isinstance(ch, dict) and ch.get("key")
    }
    refs: list[dict] = []
    for ch in snapshot_characters or []:
        if not isinstance(ch, dict):
            continue
        key = ch.get("key")
        if not isinstance(key, str) or not key:
            continue
        if key in exclude_keys or key in remix_keys:
            continue
        url = original_visual_url(ch, None)  # base variant — identity anchor
        if not url:
            continue
        refs.append({"image_url": url, "name": ch.get("name") or key})
        if len(refs) >= cap:
            break
    return refs
