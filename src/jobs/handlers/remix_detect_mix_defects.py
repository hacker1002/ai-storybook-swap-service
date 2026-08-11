"""Handler `remix_detect_mix_defects` — inspect every SWAPPED crop sheet of ONE
mix BATCH (`remixes.mixes[]`, identified by `id`) for swap defects via the AI
core `run_detect_mix_defects` ([api/remix/07]), returning the located defect
regions per sheet through `background_jobs.result.defectsBySheet`.

MIX-plane sibling of `remix_detect_defects` (job 11, sprite plane). Differences:
  - loops `mixes[batch].crop_sheets[]` (NOT `sprites[].crop_sheets[]`), scoped by
    `batch_id` (NOT `sprite_id`);
  - the body is resolved through the SHARED mix-swap resolver (job 05) — the
    constant batch lineup → `swap_targets[]` resolved ONCE, projected per sheet
    via `select_sheet_targets`; the per-cell roster + runtime annotation mirror
    `remix_mix_swap._build_core_request`;
  - `result_crops[]` is the per-cell SWAPPED pieces from `selectedSwap.crops[]`
    (LEAN `{spread_id, id, media_url}` since the 2026-06-12 reshape) JOINED to
    `original_crops[]` geometry by `(spread_id, id)` — the core recomposes the
    RESULT sheet from these, pixel-aligned with the ORIGINAL;
  - core = `run_detect_mix_defects` (2 variant sheets, full-identity), NOT
    `run_detect_swap_defects`.

Same job mechanics as job 11: NO persistence to `remixes` (defects are ADVISORY /
ephemeral — this handler NEVER issues an `UPDATE remixes`); a per-sheet core
failure is NON-FATAL (recorded in `errors[]`, siblings continue); an empty
`defects` list is SUCCESS (inspected, found nothing wrong). The whole job is
`failed` only on an exception OUTSIDE the per-sheet loop (load / resolve / empty
target pool — shared by every sheet).

⚡Sequential loop (NOT job 11's `asyncio.gather`): the mix core composes 2 sheets
(ORIGINAL + RESULT) + 2 variant sheets (OLD + NEW) per sheet — far heavier than
job 11. v1 runs sheets sequentially (cancel honored at each sheet boundary). The
2 variant sheets are CONSTANT across the batch (the lineup is invariant) → a
job-level compose cache is the obvious follow-up optimization (spec 12 OQ2/OQ3),
DEFERRED for v1 — measure latency first.

PII: never log `defect.message`, raw URLs, or human data — counts + box preview
only. `result` carries no signed URL (coords + labels only — safe for realtime).

`result` shape (frontend contract — spec 12 §Result; Phase 03 depends on it):
    {
      "defectsBySheet": [
        {
          "sheet_index": int,
          "defects": [ <SwapDefect.model_dump(exclude_none=True)>, ... ],
          "swappedDimensions": {"width": int, "height": int},  # == sheet_geometry
          "defectCount": int,
          "truncated": bool,
          "hasOldVariantSheet": bool
        }, ...
      ],
      "skipped_sheets": int,
      "errors": [ {"sheet_index": int, "code": str}, ... ]
    }
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from src.db.adapter import get_adapter
from src.jobs.runner import JobContext, register
from src.models.jobs.remix_detect_mix_defects import MAX_RESULT_ERRORS
from src.models.requests.detect_mix_defects import DetectMixDefectsRequest
from src.services.ai_usage import AiCallContext
from src.services.remix.detect_mix_defects_core import run_detect_mix_defects
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import (
    find_batch_by_id,
    resolve_mix_swap_context,
    select_sheet_targets,
)
from src.services.remix.swap_mix_prompt_builder import object_mention

# Reuse the generic "selected swap" gate helpers from job 11 (a swap_result is a
# swap_result regardless of plane) — DRY, 1 source for the router precount + the
# handler scope. Re-exported so the enqueue router can import them from here.
from src.jobs.handlers.remix_detect_defects import (
    selected_swap_media_url,
    selected_swap_result,
)

logger = logging.getLogger(__name__)

__all__ = [
    "handle",
    "selected_swap_media_url",
    "selected_swap_result",
    "geometry_by_crop_key",
    "result_crops_from_swap",
    "build_detect_mix_request",
]


# Per-cell roster injection toggle (parity `remix_mix_swap.INJECT_CROP_OBJECTS`):
# variant-qualified mentions from `original_crops[].tags[]` give the detect model
# the SAME deterministic cell↔target binding the swap used. ON by default.
INJECT_CROP_OBJECTS = True


# ─── result_crops geometry join (MIX-specific — lean → full) ─────────────────


def geometry_by_crop_key(
    original_crops: Any,
) -> dict[tuple[Any, Any], dict]:
    """Map `(spread_id, id) -> geometry` from `original_crops[]`.

    The lean `selectedSwap.crops[]` carry `{spread_id, id, media_url}` only (no
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


def result_crops_from_swap(
    selected_swap: dict, original_crops: Any
) -> tuple[list[dict], int]:
    """Project lean `selectedSwap.crops[]` → `result_crops[]` for the detect core.

    Each lean piece (`{spread_id, id, media_url}`) is JOINED to `original_crops[]`
    geometry by `(spread_id, id)`. A piece with no valid http `media_url`, or
    whose `(spread_id, id)` has no matching geometry, is DROPPED. Returns
    `(result_crops, missing_count)`; `missing_count` = pieces dropped for a failed
    geometry join (logged by the caller as a count mismatch — spec OQ8). The
    output `Crop` shape is `{id, media_url, geometry}` (the build-crop-sheet model
    is `extra="forbid"`, so only those keys are emitted).
    """
    geom_by_key = geometry_by_crop_key(original_crops)
    out: list[dict] = []
    missing = 0
    for idx, c in enumerate(selected_swap.get("crops") or []):
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


# ─── per-cell roster + annotation (parity remix_mix_swap) ────────────────────


def _build_annotation_map(
    illustration: dict | None,
) -> dict[tuple[Any, Any], dict]:
    """Runtime annotation map keyed `(spread_id, image_id)` (mirror
    `remix_mix_swap._build_annotation_map`) — crops no longer clone `annotation`
    (DB lean shape 2026-06-12). Only non-empty annotations are kept."""
    out: dict[tuple[Any, Any], dict] = {}
    for spread in (illustration or {}).get("spreads") or []:
        if not isinstance(spread, dict):
            continue
        spread_id = spread.get("id")
        for img in spread.get("images") or []:
            if not isinstance(img, dict):
                continue
            annotation = img.get("annotation")
            if isinstance(annotation, dict) and annotation:
                out[(spread_id, img.get("id"))] = annotation
    return out


def _crop_object_mentions(crop: dict) -> list[str]:
    """Variant-qualified mentions of every object tagged in ONE crop (parity
    `remix_mix_swap._crop_object_mentions`). Deduped + sorted for determinism;
    byte-identical to the image-guide label inner text (deterministic binding)."""
    seen: set[str] = set()
    for tag in crop.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        object_key = (tag.get("object_key") or "").strip()
        if not object_key:
            continue
        seen.add(object_mention(object_key, tag.get("variant_key") or "base"))
    return sorted(seen)


# ─── per-sheet request projection ────────────────────────────────────────────


def build_detect_mix_request(
    sheet: dict,
    sheet_targets: list[dict],
    result_crops: list[dict],
    annotation_map: dict[tuple[Any, Any], dict],
    controls: dict,
    *,
    sheet_focus: Optional[list[str]] = None,
) -> DetectMixDefectsRequest:
    """Project a stored mix crop sheet + its resolved swap targets into the
    detect-mix core request (superset of the swap-mix body + result pieces).

    `crops[]` come from `original_crops[]` (geometry/media_url + runtime
    annotation keyed `(spread_id, id)` + per-cell roster from tags);
    `swap_targets` = the per-sheet subset (resolver dicts coerced by Pydantic);
    `result_crops` = the lean→full join. `original_sheet_url` is set ONLY when the
    sheet already carries a `composed_sheet_url` (fast-path — skip the ORIGINAL
    compose); otherwise the core composes the ORIGINAL from `crops[]`. Controls
    that are None are dropped so the core defaults apply. `sheet_focus` is the
    focus list ALREADY intersected with this sheet's target keys (the request
    validator enforces `focus_objects ⊆ swap_targets[].key`)."""
    geom = sheet.get("sheet_geometry") or {}

    crops: list[dict] = []
    for idx, c in enumerate(sheet.get("original_crops") or []):
        if not isinstance(c, dict) or not isinstance(c.get("geometry"), dict):
            continue
        mu = c.get("media_url")
        if not (isinstance(mu, str) and mu.startswith("http")):
            continue
        g = c["geometry"]
        annotation = annotation_map.get((c.get("spread_id"), c.get("id"))) or None
        roster = (_crop_object_mentions(c) or None) if INJECT_CROP_OBJECTS else None
        crops.append(
            {
                "id": c.get("id") or f"crop-{idx}",
                "media_url": mu,
                "geometry": {
                    "x": int(g.get("x", 0)),
                    "y": int(g.get("y", 0)),
                    "w": int(g.get("w", 0)),
                    "h": int(g.get("h", 0)),
                },
                "annotation": annotation,
                "objects": roster,
            }
        )

    kwargs: dict[str, Any] = {
        "sheet_geometry": {
            "width": int(geom.get("width", 0)),
            "height": int(geom.get("height", 0)),
        },
        "crops": crops,
        "swap_targets": sheet_targets,
        "result_crops": result_crops,
    }
    # Fast-path: a pre-composed ORIGINAL sheet (skip compose + crop fetch).
    composed = sheet.get("composed_sheet_url")
    if isinstance(composed, str) and composed.startswith("http"):
        kwargs["original_sheet_url"] = composed
    # CONTEXT-only knobs (rendered into builder_params; never call Gemini).
    if controls.get("swap_model") is not None:
        kwargs["swap_model"] = controls["swap_model"]
    if controls.get("swap_temperature") is not None:
        kwargs["swap_temperature"] = controls["swap_temperature"]
    # Detect controls — omit None so core defaults (severity 'low', max 30) apply.
    if sheet_focus is not None:
        kwargs["focus_objects"] = sheet_focus
    if controls.get("severity_threshold") is not None:
        kwargs["severity_threshold"] = controls["severity_threshold"]
    if controls.get("max_defects") is not None:
        kwargs["max_defects"] = controls["max_defects"]
    return DetectMixDefectsRequest(**kwargs)


# ─── result/error helpers (mirror job 11) ────────────────────────────────────


def _build_result(
    defects_by_sheet: list[dict], skipped_sheets: int, errors: list[dict]
) -> dict[str, Any]:
    return {
        "defectsBySheet": defects_by_sheet,
        "skipped_sheets": skipped_sheets,
        "errors": errors,
    }


def _push_error(errors: list[dict], code: str, *, sheet_index: Optional[int] = None) -> None:
    """Append a lean error entry (code + optional sheet_index). PII-safe — NO
    message/url/human data. Capped at `MAX_RESULT_ERRORS`."""
    if len(errors) >= MAX_RESULT_ERRORS:
        return
    entry: dict[str, Any] = {"code": code}
    if sheet_index is not None:
        entry["sheet_index"] = sheet_index
    errors.append(entry)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RemixDomainError):
        return exc.code or "INTERNAL"
    return "INTERNAL"


# ─── handler ─────────────────────────────────────────────────────────────────


@register("remix_detect_mix_defects")
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    batch_id: str = params["batch_id"]
    controls: dict = params.get("controls") or {}

    # AI-usage attribution (Phase 05): built from the JOB ROW (mirror remix_sprite_swap).
    ai_ctx = AiCallContext(
        job_id=job["id"],
        user_id=job.get("user_id"),
        book_id=job.get("book_id"),
        remix_id=params.get("remix_id"),
        snapshot_id=params.get("snapshot_id"),
        admin_ref=params.get("admin_ref"),
        sid=params.get("sid"),
    )

    adapter = get_adapter()
    defects_by_sheet: list[dict] = []
    errors: list[dict] = []

    # ── Load remix fresh (do NOT trust the enqueue snapshot) ─────────
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception:  # noqa: BLE001
        logger.exception("detect_mix_remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    if not remix:
        _push_error(errors, "REMIX_NOT_FOUND")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    remix_characters: list = remix.get("characters") or []
    remix_props: list = remix.get("props") or []
    remix_sprites: list = remix.get("sprites") or []
    mixes: list = remix.get("mixes") or []
    snapshot_id = remix.get("snapshot_id")
    annotation_map = _build_annotation_map(remix.get("illustration"))

    batch = find_batch_by_id(mixes, batch_id)
    if batch is None:
        _push_error(errors, "BATCH_NOT_FOUND")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    # ── Load snapshot + resolve shared target pool ONCE (fresh) ──────
    #    The batch lineup is constant across every sheet → the swap_targets pool
    #    is shared, so a resolve failure / empty pool / missing char ref is a
    #    BATCH-level fault (the whole job fails — parity remix_mix_swap resolve).
    try:
        snap_characters: list = []
        snap_props: list = []
        if snapshot_id:
            snap = await adapter.get_snapshot(snapshot_id)
            snap_characters = (snap or {}).get("characters") or []
            snap_props = (snap or {}).get("props") or []

        mix_ctx = resolve_mix_swap_context(
            batch,
            remix_characters,
            remix_props,
            snap_characters,
            snap_props,
            remix_sprites=remix_sprites,
        )
        # An ENABLED char without a sprite final = half-applied swap state; the
        # shared pool is broken for every sheet → whole job fails (spec 12 flow).
        if mix_ctx.missing_char_refs:
            raise RemixDomainError(
                status=422,
                code="MISSING_OBJECT_CONFIG",
                message="character tokens missing a sprite final reference",
                details={"tokens": mix_ctx.missing_char_refs},
            )
        if not mix_ctx.target_map:
            raise RemixDomainError(
                status=422,
                code="MISSING_OBJECT_CONFIG",
                message="batch has no token resolvable to a swap target",
            )
        target_map = mix_ctx.target_map
    except RemixDomainError as exc:
        _push_error(errors, exc.code or "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))
    except Exception:  # noqa: BLE001
        logger.exception("detect_mix_resolve_failed remix_id=%s", remix_id)
        _push_error(errors, "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    crop_sheets: list = batch.get("crop_sheets") or []
    skipped = 0
    done = 0
    cancelled = False
    sheets_block: dict[str, Any] = {}
    step_details: dict[str, Any] = {"sheets": sheets_block}

    logger.info(
        "remix_detect_mix_defects_start job_id=%s remix_id=%s sheets=%d targets=%d",
        ctx.id, remix_id, len(crop_sheets), mix_ctx.target_count,
    )

    # ── Sequential per-sheet loop (cancel at each sheet boundary) ────
    for i, sheet in enumerate(crop_sheets):
        if await ctx.check_cancel():
            cancelled = True
            break
        if not isinstance(sheet, dict):
            continue

        # Gate 1 — selected swap (the AFTER image to inspect). No swap → NOT in
        # the total_steps denominator → skip without advancing `done`.
        selected_swap = selected_swap_result(sheet)
        if selected_swap is None:
            skipped += 1
            continue

        # Gate 2 — this sheet's resolved target subset (objects actually present).
        sheet_targets = select_sheet_targets(target_map, sheet)
        if not sheet_targets:
            skipped += 1
            continue

        # Gate 3 — the per-cell SWAPPED pieces (lean) joined to geometry.
        result_crops, join_missing = result_crops_from_swap(
            selected_swap, sheet.get("original_crops")
        )
        if join_missing:
            # PII-safe: counts only (no url/key). Spec OQ8 — a piece whose cell
            # key has no original geometry is dropped (legacy/partial row).
            logger.info(
                "detect_mix_result_crops_join_mismatch job_id=%s sheet_index=%d dropped=%d",
                ctx.id, i, join_missing,
            )
        if not result_crops:
            logger.info(
                "detect_mix_sheet_skipped_no_result_crops job_id=%s sheet_index=%d", ctx.id, i
            )
            skipped += 1
            continue

        # Gate 4 — focus intersect. The request validator enforces
        # `focus_objects ⊆ swap_targets[].key`, so a batch-level focus must be
        # narrowed to THIS sheet's targets. When focus is set but none of the
        # focused tokens are present here, there is nothing focusable to inspect →
        # record an empty result WITHOUT a Gemini call (correct + saves cost; an
        # empty list would otherwise read as "no filter" in the core engine).
        sheet_focus: Optional[list[str]] = None
        focus = controls.get("focus_objects")
        key = str(i)
        if focus is not None:
            target_keys = {t["key"] for t in sheet_targets}
            sheet_focus = [k for k in focus if k in target_keys]
            if not sheet_focus:
                geom = sheet.get("sheet_geometry") or {}
                defects_by_sheet.append(
                    {
                        "sheet_index": i,
                        "defects": [],
                        "swappedDimensions": {
                            "width": int(geom.get("width", 0)),
                            "height": int(geom.get("height", 0)),
                        },
                        "defectCount": 0,
                    }
                )
                sheets_block[key] = {"state": "done", "defect_count": 0}
                done += 1
                await ctx.report(current_step=done, step_details=step_details)
                continue

        # ── Inspect (in scope) ──────────────────────────────────────
        sheets_block[key] = "running"
        try:
            req = build_detect_mix_request(
                sheet, sheet_targets, result_crops, annotation_map, controls,
                sheet_focus=sheet_focus,
            )
            res = await run_detect_mix_defects(req, ai_context=ai_ctx)
            dims = res.meta.swappedDimensions
            defects_by_sheet.append(
                {
                    "sheet_index": i,
                    "defects": [d.model_dump(exclude_none=True) for d in res.defects],
                    "swappedDimensions": {"width": dims.width, "height": dims.height},
                    "defectCount": res.meta.defectCount,
                    "truncated": bool(res.meta.truncated),
                    "hasOldVariantSheet": bool(res.meta.hasOldVariantSheet),
                }
            )
            sheets_block[key] = {"state": "done", "defect_count": res.meta.defectCount}
        except RemixDomainError as exc:
            _push_error(errors, _error_code(exc), sheet_index=i)
            sheets_block[key] = {"state": "failed", "code": _error_code(exc)}
        except Exception:  # noqa: BLE001 — per-sheet isolation, non-fatal
            logger.exception(
                "detect_mix_sheet_unexpected job_id=%s sheet_index=%d", ctx.id, i
            )
            _push_error(errors, "INTERNAL", sheet_index=i)
            sheets_block[key] = {"state": "failed", "code": "INTERNAL"}
        done += 1
        await ctx.report(current_step=done, step_details=step_details)

    # Cooperative cancel: keep inspected work when any sheet completed (parity
    # job 11 / job 02) — only 'cancelled' when nothing was inspected.
    if cancelled and not defects_by_sheet:
        return ("cancelled", _build_result(defects_by_sheet, skipped, errors))

    logger.info(
        "remix_detect_mix_defects_done job_id=%s inspected=%d skipped=%d errors=%d cancelled=%s",
        ctx.id, len(defects_by_sheet), skipped, len(errors), cancelled,
    )
    return ("completed", _build_result(defects_by_sheet, skipped, errors))
