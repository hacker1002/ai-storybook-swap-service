"""Shared resolver — remix `rmbgs[]` row → bodies for the rmbg crop pipeline.

Ported VERBATIM from `ai-storybook-image-api/src/services/jobs/remix_rmbg_resolver.py`
(P3b). PURE — no DB/storage/logging — so it is copied byte-for-byte (no seam swap).

ONE place that projects a `remixes.rmbgs[]` crop sheet into the shapes both rmbg
jobs need, so the rmbg-swap handler (job 09) and the detect-rmbg-defects handler
(job 13) can never drift:

  - `compose_crop_entries(original_crops)` — the still-background pieces
    (`original_crops[]`, BEFORE remove-bg) projected into parallel
    `(compose_crops, cut_crops)` inputs. Job 09 composes `compose_crops` into the
    PLAIN sheet it feeds remove-bg + cuts by `cut_crops`; job 13 sends
    `compose_crops` as the detect ORIGINAL (SUBJECT-vs-BACKGROUND reference).
    EXTRACTED VERBATIM from `remix_rmbg._compose_crop_entries` (behaviour-identical
    — job 09 now imports it) so the two never disagree.

  - `resolve_detect_rmbg_body(sheet, selected, controls)` — assembles the full
    `DetectRmbgDefectsRequest` for job 13:
      · `crops`            = `compose_crop_entries(original_crops)[0]`  (ORIGINAL)
      · `result_crops`     = `selected.crops[]` JOINed to `original_crops[]`
                             geometry by `(spread_id, id)` (lean → full, mirror
                             job 12) — the per-cell RGBA pieces the core recomposes
                             into the RESULT when the fast-path sheet is absent.
      · `result_sheet_url` = `selected.media_url` (persisted RGBA sheet — the core
                             FAST-PATH AFTER image; skips the result recompose).
      · `sheet_geometry`   = `sheet.sheet_geometry`.

The lean `selected.crops[]` carry `{spread_id, id, media_url}` only (no geometry
since the 2026-06-12 lean reshape), so the geometry needed to recompose the RESULT
is joined back from `original_crops[]` on the invariant `(spread_id, id)` cell key.

PURE — no DB I/O, no logging (logging is the caller's concern); trivially
unit-testable + never blocks the event loop. Returns structured data only — the
core does not log URLs either (PII discipline parity 08/09).
"""

from __future__ import annotations

from typing import Any

from src.models.requests.detect_rmbg_defects import DetectRmbgDefectsRequest

__all__ = [
    "compose_crop_entries",
    "geometry_by_crop_key",
    "result_crops_from_selected",
    "resolve_detect_rmbg_body",
]


def compose_crop_entries(
    stored_crops: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project stored `original_crops[]` into parallel composer + cut inputs.

    Returns `(compose_crops, cut_crops)` — index-aligned, sharing the SAME id
    (synthesized `crop-{idx}` when absent) so a composer `skipped[].id` maps
    1:1 onto the cut crop to drop. Entries without a dict geometry or an http
    media_url are excluded from BOTH lists (the composer would reject them;
    cutting their cell would slice blank gutter).

    `compose_crops[]` (`{id, media_url, geometry}`) doubles as the detect-rmbg
    ORIGINAL `crops[]` (job 13). EXTRACTED VERBATIM from
    `remix_rmbg._compose_crop_entries` — keep behaviour-identical (job 09 imports
    this; its regression suite guards the move).
    """
    compose_crops: list[dict[str, Any]] = []
    cut_crops: list[dict[str, Any]] = []
    for idx, c in enumerate(stored_crops):
        if not isinstance(c, dict) or not isinstance(c.get("geometry"), dict):
            continue
        media_url = c.get("media_url")
        if not isinstance(media_url, str) or not media_url.startswith("http"):
            continue
        g = c["geometry"]
        geometry = {
            "x": int(g.get("x", 0)),
            "y": int(g.get("y", 0)),
            "w": int(g.get("w", 0)),
            "h": int(g.get("h", 0)),
        }
        crop_id = c.get("id") or f"crop-{idx}"
        compose_crops.append(
            {"id": crop_id, "media_url": media_url, "geometry": geometry}
        )
        cut_crops.append(
            {"spread_id": c.get("spread_id"), "id": crop_id, "geometry": geometry}
        )
    return compose_crops, cut_crops


def geometry_by_crop_key(
    original_crops: Any,
) -> dict[tuple[Any, Any], dict]:
    """Map `(spread_id, id) -> geometry` from `original_crops[]` (mirror job 12).

    The lean `selected.crops[]` carry `{spread_id, id, media_url}` only (no
    geometry — DB lean reshape 2026-06-12), so the geometry needed to recompose
    the RESULT sheet is joined back from `original_crops[]` on the invariant
    `(spread_id, id)` cell key.
    """
    out: dict[tuple[Any, Any], dict] = {}
    for c in original_crops or []:
        if not isinstance(c, dict):
            continue
        geom = c.get("geometry")
        if isinstance(geom, dict):
            out[(c.get("spread_id"), c.get("id"))] = geom
    return out


def result_crops_from_selected(
    selected: dict, original_crops: Any
) -> tuple[list[dict], int]:
    """Project lean `selected.crops[]` → `result_crops[]` for the detect-rmbg core.

    Each lean piece (`{spread_id, id, media_url}`) is JOINED to `original_crops[]`
    geometry by `(spread_id, id)`. A piece with no valid http `media_url`, or
    whose `(spread_id, id)` has no matching geometry, is DROPPED. Returns
    `(result_crops, missing_count)`; `missing_count` = pieces dropped for a failed
    geometry join. The output crop shape is `{id, media_url, geometry}`
    (`RmbgCrop` is `extra="forbid"`, so only those keys are emitted).
    """
    geom_by_key = geometry_by_crop_key(original_crops)
    out: list[dict] = []
    missing = 0
    for idx, c in enumerate(selected.get("crops") or []):
        if not isinstance(c, dict):
            continue
        mu = c.get("media_url")
        if not (isinstance(mu, str) and mu.startswith("http")):
            continue
        geom = geom_by_key.get((c.get("spread_id"), c.get("id")))
        if not isinstance(geom, dict):
            missing += 1
            continue
        out.append(
            {
                "id": str(c.get("id") or f"piece-{idx}"),
                "media_url": mu,
                "geometry": {
                    "x": int(geom.get("x", 0)),
                    "y": int(geom.get("y", 0)),
                    "w": int(geom.get("w", 0)),
                    "h": int(geom.get("h", 0)),
                },
            }
        )
    return out, missing


def resolve_detect_rmbg_body(
    sheet: dict, selected: dict, controls: dict
) -> DetectRmbgDefectsRequest:
    """Assemble the per-sheet `DetectRmbgDefectsRequest` (job 13).

    `crops` = ORIGINAL still-bg pieces (`original_crops[]` projection);
    `result_crops` = lean→full geometry join of `selected.crops[]`;
    `result_sheet_url` = `selected.media_url` (persisted RGBA — core fast-path AFTER
    image; set ONLY when it is a valid http url, else the core recomposes from
    `result_crops[]`). `sheet_geometry` from the sheet. Detect controls that are
    None are dropped so the core defaults apply (severity 'low', max_defects 30).

    Raises `RemixDomainError(400, ...)` via the request model_validator when the
    projected `crops` / `result_crops` are empty or out of bounds — the caller
    (handler) catches this per-sheet (advisory, non-fatal).
    """
    geom = sheet.get("sheet_geometry") or {}
    original_crops = sheet.get("original_crops") or []
    compose_crops, _cut = compose_crop_entries(original_crops)
    result_crops, _missing = result_crops_from_selected(selected, original_crops)

    kwargs: dict[str, Any] = {
        "sheet_geometry": {
            "width": int(geom.get("width", 0)),
            "height": int(geom.get("height", 0)),
        },
        "crops": compose_crops,
        "result_crops": result_crops,
    }
    # FAST-PATH: the persisted RGBA result sheet (job 09 PERSIST → skip recompose).
    media_url = selected.get("media_url")
    if isinstance(media_url, str) and media_url.startswith("http"):
        kwargs["result_sheet_url"] = media_url
    # Detect controls — omit None so core defaults apply.
    if controls.get("severity_threshold") is not None:
        kwargs["severity_threshold"] = controls["severity_threshold"]
    if controls.get("max_defects") is not None:
        kwargs["max_defects"] = controls["max_defects"]
    return DetectRmbgDefectsRequest(**kwargs)
