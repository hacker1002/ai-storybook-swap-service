"""Pure resolver for sprite-swap finals → reference-URL map (mix-swap source).

Port of the FE helper `ai-storybook-editor/src/stores/remix-store/
apply-sprite-finals.ts::resolveSpriteFinals`. Reads ONLY the winning swapped
crop per cell across all sprites:
    sprites[].crop_sheets[].swap_results[is_selected].crops[is_final]

The cell key is `${type}/${object_key}/${variant_key}` — the SAME grammar the
mix-swap resolver builds from a lineup token (`parse_token` normalizes a
null/empty variant to 'base'). Base-variant crops in the wild may carry either
the literal `'base'` or `null`/empty for `variant_key`; we normalize null/empty
→ 'base' here so the key matches the mix-token convention on both sides.

Tie-break (defensive — at steady state exactly one final per cell):
  - highest `sprite.order` wins;
  - on equal order, the lexicographically SMALLER `sprite.id` wins.

PURE: no I/O, no logging (the caller — handler/router — owns observability).
This replaces the removed `variant.visual_swap_url` bridge: the FE no longer
writes that field; the reference URL is read directly from the sprite finals.
"""

from __future__ import annotations

import dataclasses

__all__ = ["resolve_sprite_finals"]


@dataclasses.dataclass(slots=True)
class _Pending:
    media_url: str
    order: float
    sprite_id: str


def _normalize_variant_key(value: object) -> str:
    """Match the mix-token convention: null/empty variant_key → 'base'."""
    if value is None:
        return "base"
    text = str(value).strip()
    return text or "base"


def _selected_sheet_crops(sheet: object) -> list:
    """Return the crops of the `is_selected` swap_result of a sheet (or [])."""
    if not isinstance(sheet, dict):
        return []
    swap_results = sheet.get("swap_results")
    if not isinstance(swap_results, list):
        return []
    for result in swap_results:
        if isinstance(result, dict) and result.get("is_selected"):
            crops = result.get("crops")
            return crops if isinstance(crops, list) else []
    return []


def resolve_sprite_finals(sprites: list | None) -> dict[str, str]:
    """Resolve the `is_final` winner crop per cell across all sprites.

    Returns a map `cellKey -> media_url` where `cellKey` is
    `${type}/${object_key}/${variant_key}` (variant normalized null→'base').

    Reads ONLY `sprites[].crop_sheets[].swap_results[is_selected].crops[is_final]`.
    On >1 final for the same cell (invariant breach), the highest `sprite.order`
    wins; ties broken by the smaller `sprite.id` (lexicographic). PURE.
    """
    result: dict[str, _Pending] = {}

    for sprite in sprites or []:
        if not isinstance(sprite, dict):
            continue
        crop_sheets = sprite.get("crop_sheets")
        if not isinstance(crop_sheets, list):
            continue

        sprite_id = str(sprite.get("id") or "")
        # `order` may be missing/non-numeric in malformed rows → treat as 0.
        raw_order = sprite.get("order")
        order = float(raw_order) if isinstance(raw_order, (int, float)) else 0.0

        for sheet in crop_sheets:
            for crop in _selected_sheet_crops(sheet):
                if not isinstance(crop, dict) or crop.get("is_final") is not True:
                    continue
                type_str = crop.get("type")
                object_key = crop.get("object_key")
                media_url = crop.get("media_url")
                if not type_str or not object_key or not media_url:
                    continue
                variant_key = _normalize_variant_key(crop.get("variant_key"))
                key = f"{type_str}/{object_key}/{variant_key}"

                candidate = _Pending(
                    media_url=media_url, order=order, sprite_id=sprite_id
                )
                existing = result.get(key)
                if existing is None:
                    result[key] = candidate
                    continue
                challenger_wins = candidate.order > existing.order or (
                    candidate.order == existing.order
                    and candidate.sprite_id < existing.sprite_id
                )
                if challenger_wins:
                    result[key] = candidate

    return {key: pending.media_url for key, pending in result.items()}
