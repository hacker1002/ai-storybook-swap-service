"""Handler `remix_detect_defects` — inspect every SWAPPED crop sheet of ONE sprite
for swap defects via the AI core `run_detect_swap_defects`, returning the located
defect regions per sheet through `background_jobs.result.defectsBySheet`.

A 1:1 MIRROR of `remix_sprite_swap` EXCEPT:
  (a) NO persistence to `remixes` — defects are ADVISORY / ephemeral; the runner
      writes the returned `result` to `background_jobs.result` only. This handler
      NEVER issues an `UPDATE remixes` (single-writer safety — grep-verified).
  (b) sheets run CONCURRENT (`asyncio.gather`), NOT sequential — detect has no
      per-sheet DB write so the sheets are independent. The in-flight Gemini-flash
      calls are bounded by the core's OWN `_DETECT_SEM=3` (a SEPARATE pool from
      the image-gen swap semaphore), so the handler adds NO semaphore of its own.
  (c) `current_step` is a MONOTONIC completed-count (incremented in `finally`),
      NOT the loop index — gather completion order is non-deterministic.

Scope = every crop sheet carrying an `is_selected` swap_result whose `crops[]`
project to usable `result_crops` (the per-cell SWAPPED pieces the core recomposes
into the RESULT sheet, pixel-aligned with the ORIGINAL). Sheets without a selected
swap — or whose selected swap has no usable crop pieces — are skipped (counted in
`skipped_sheets`). A per-sheet core failure is NON-FATAL —
recorded in `errors[]` with its code; sibling sheets continue. An empty `defects`
list is SUCCESS (inspected, found nothing wrong) — NOT an error. The whole job is
`failed` only on an exception OUTSIDE the per-sheet loop (load / resolve).

PII: never log `defect.message`, raw URLs, or human data — counts + box preview
only. `result` carries no signed URL.

`result` shape (frontend contract — Phase 03/04 depend on it):
    {
      "defectsBySheet": [
        {
          "sheet_index": int,
          "defects": [ <SwapDefect.model_dump(exclude_none=True)>, ... ],
          "swappedDimensions": {"width": int, "height": int},
          "defectCount": int,
          "truncated": bool
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

from src.core.job_types import JOB_TYPE_DETECT_DEFECTS
from src.db.adapter import get_adapter
from src.jobs.runner import JobContext, register
from src.models.jobs.remix_detect_defects import MAX_RESULT_ERRORS
from src.models.requests.detect_swap_defects import DetectSwapDefectsRequest
from src.services.ai_usage import AiCallContext
from src.services.remix.detect_swap_defects_core import run_detect_swap_defects
from src.services.remix.errors import RemixDomainError
from src.services.remix.sprite_swap_resolver import (
    eligible_sheet_crops as _eligible_sheet_crops,
)
from src.services.remix.sprite_swap_resolver import (
    find_sprite_by_id,
    resolve_sprite_object_map,
    select_sheet_objects,
)

logger = logging.getLogger(__name__)

__all__ = ["handle", "selected_swap_media_url", "selected_swap_result", "result_crops_from_swap"]


# ─── shared scope helpers (router precount + handler scope build) ─────────────


def selected_swap_result(sheet: dict) -> Optional[dict]:
    """Return this sheet's SELECTED swap_result dict (the swapped AFTER state), or
    None when no selected swap carries a valid http `media_url`.

    SINGLE source for both the "is this sheet swapped?" gate AND the `result_crops`
    projection — deriving the gate media_url and the crops[] from the SAME swap
    result means they can never come from different swaps (the lean-source risk).
    """
    if not isinstance(sheet, dict):
        return None
    for r in sheet.get("swap_results") or []:
        if isinstance(r, dict) and r.get("is_selected"):
            mu = r.get("media_url")
            if isinstance(mu, str) and mu.startswith("http"):
                return r
    return None


def selected_swap_media_url(sheet: dict) -> Optional[str]:
    """http `media_url` of this sheet's selected swap result (the AFTER image), or
    None. Gate helper — used by the enqueue router precount (→ NO_SWAP_RESULT) and
    derived from `selected_swap_result` (1 source → router + handler never disagree).
    """
    r = selected_swap_result(sheet)
    if r is None:
        return None
    mu = r.get("media_url")
    return mu if isinstance(mu, str) else None


# Whitelist of the 5 `SpriteSheetCrop` fields — `recut_crops` already emit exactly
# these; the handler adds `is_final` (+ possibly other extras). `SpriteSheetCrop` is
# extra="forbid", so projection must strip everything else.
_CROP_FIELDS = ("type", "object_key", "variant_key", "media_url", "geometry")


def result_crops_from_swap(selected_swap: dict) -> list[dict]:
    """Project `selectedSwap.crops[]` (= `recut_crops`) → clean `result_crops[]` for
    `DetectSwapDefectsRequest`.

    `recut_crops` carry `{type, object_key, variant_key, geometry, media_url}` and
    the handler adds `is_final`; this strips extras (whitelist `_CROP_FIELDS`) so the
    extra="forbid" model never raises. A crop missing a valid http `media_url` or a
    `geometry` is dropped (legacy/partial rows) — the resulting list may be empty,
    in which case the caller skips the sheet (advisory, non-fatal).
    """
    out: list[dict] = []
    for c in selected_swap.get("crops") or []:
        if not isinstance(c, dict):
            continue
        mu = c.get("media_url")
        if not (isinstance(mu, str) and mu.startswith("http")) or not c.get("geometry"):
            continue
        out.append({k: c[k] for k in _CROP_FIELDS if k in c})
    return out


# ─── per-sheet request projection (mirror remix_sprite_swap field-by-field) ──


def build_detect_request(
    sheet: dict,
    sheet_objects: list[dict],
    eligible_crops: list[dict[str, Any]],
    result_crops: list[dict[str, Any]],
    controls: dict,
) -> DetectSwapDefectsRequest:
    """Project a stored SWAPPED sprite sheet + its resolved swap objects into the
    detect core request.

    Field-for-field mirror of `remix_sprite_swap._build_core_request`: same
    `sheet_geometry` + eligible `crops[]` (ORIGINAL artwork) + per-sheet
    `swap_objects` (from `select_sheet_objects`), PLUS `result_crops[]` (the per-cell
    SWAPPED pieces — projected from the selected swap result; the core recomposes the
    RESULT sheet from these) and the optional detect controls. `original_sheet_url`
    is intentionally OMITTED (v1) — the core re-composes the ORIGINAL from
    `crops[].media_url` per sheet. Controls that are None are dropped so the model's
    own defaults apply. `crops` ↔ `result_crops` share geometry/order → pixel-aligned.
    """
    geom = sheet.get("sheet_geometry") or {}
    kwargs: dict[str, Any] = {
        "sheet_geometry": {
            "width": int(geom.get("width", 0)),
            "height": int(geom.get("height", 0)),
        },
        "crops": eligible_crops,
        "swap_objects": sheet_objects,
        "result_crops": result_crops,
    }
    # CONTEXT-only knobs (rendered into builder_params; never call Gemini).
    if controls.get("swap_model") is not None:
        kwargs["swap_model"] = controls["swap_model"]
    if controls.get("swap_temperature") is not None:
        kwargs["swap_temperature"] = controls["swap_temperature"]
    # Detect controls — omit None so model defaults (severity 'low', max 30) apply.
    if controls.get("focus_objects") is not None:
        kwargs["focus_objects"] = controls["focus_objects"]
    if controls.get("severity_threshold") is not None:
        kwargs["severity_threshold"] = controls["severity_threshold"]
    if controls.get("max_defects") is not None:
        kwargs["max_defects"] = controls["max_defects"]
    return DetectSwapDefectsRequest(**kwargs)


# ─── result/error helpers ────────────────────────────────────────────────────


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


@register(JOB_TYPE_DETECT_DEFECTS)
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    sprite_id: str = params["sprite_id"]
    controls: dict = params.get("controls") or {}

    # AI-usage attribution (Phase 05): built from the JOB ROW (mirror remix_sprite_swap).
    # `remix_id` routes the Gemini detect cost to the remix billing bucket.
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("detect_remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    if not remix:
        _push_error(errors, "REMIX_NOT_FOUND")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    remix_config: dict = remix.get("remix_config") or {}
    sprites: list = remix.get("sprites") or []
    snapshot_id = remix.get("snapshot_id")

    sprite = find_sprite_by_id(sprites, sprite_id)
    if sprite is None:
        _push_error(errors, "SPRITE_NOT_FOUND")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    # ── Load snapshot characters + humans → resolve object pool ONCE ──
    #    (P3b seam: snapshot by id via `get_snapshot`; humans read GLOBALLY via
    #    `list_humans` then filtered by the referenced ids — the App DB has no
    #    per-id humans read and `humans` has no book_id column.)
    try:
        snap_characters: list = []
        if snapshot_id:
            snap = await adapter.get_snapshot(snapshot_id)
            snap_characters = (snap or {}).get("characters") or []

        rc_chars = remix_config.get("characters") or []
        human_ids = sorted(
            {
                c["human_id"]
                for c in rc_chars
                if isinstance(c, dict) and c.get("human_id")
            }
        )
        humans_by_id: dict[str, dict] = {}
        if human_ids:
            wanted = set(human_ids)
            # humans.id comes back as uuid.UUID (pool has no uuid text-codec) while
            # remix_config human_id is str -> coerce to str before matching, else the
            # lookup silently misses and swap precondition fails (mirror sprite_swap).
            for row in await adapter.list_humans(job.get("book_id")) or []:
                if isinstance(row, dict) and row.get("id") is not None:
                    rid = str(row["id"])
                    if rid in wanted:
                        humans_by_id[rid] = row

        pool = resolve_sprite_object_map(
            sprite, remix_config, humans_by_id, snap_characters
        )
        if not pool.lineup:
            raise RemixDomainError(
                status=422,
                code="NO_SWAP_OBJECTS",
                message="sprite has no character cell",
            )
        if pool.missing:
            raise RemixDomainError(
                status=422,
                code="MISSING_OBJECT_CONFIG",
                message="one or more sprite objects are missing remix_config",
                details={"object_keys": pool.missing},
            )
        object_map = pool.object_map
    except RemixDomainError as exc:
        _push_error(errors, exc.code or "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))
    except Exception as exc:  # noqa: BLE001
        logger.exception("detect_resolve_object_map_failed remix_id=%s", remix_id)
        _push_error(errors, "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    # ── Build scope: every sheet with a selected swap that yields usable
    #    `result_crops` (the per-cell SWAPPED pieces the core recomposes). A sheet
    #    whose selected swap carries no valid crop pieces (legacy/partial) is SKIPPED
    #    (folded into skipped_sheets) — advisory, non-fatal. ──
    crop_sheets: list = sprite.get("crop_sheets") or []
    scope: list[tuple[int, dict, list[dict]]] = []
    for i, sheet in enumerate(crop_sheets):
        if not isinstance(sheet, dict):
            continue
        selected_swap = selected_swap_result(sheet)
        if selected_swap is None:
            continue
        result_crops = result_crops_from_swap(selected_swap)
        if not result_crops:
            logger.info(
                "detect_sheet_skipped_no_result_crops job_id=%s sheet_index=%d", ctx.id, i
            )
            continue
        scope.append((i, sheet, result_crops))
    skipped_sheets = len(crop_sheets) - len(scope)

    if not scope:
        # Enqueue guards NO_SWAP_RESULT; defensive — nothing to inspect.
        return (
            "completed",
            _build_result(defects_by_sheet, skipped_sheets, errors),
        )

    logger.info(
        "remix_detect_defects_start job_id=%s remix_id=%s sheets=%d skipped=%d objects=%d",
        ctx.id, remix_id, len(scope), skipped_sheets, pool.object_count,
    )

    # Cooperative cancel — pre-gather boundary (nothing inspected yet).
    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(defects_by_sheet, skipped_sheets, errors),
        )

    sheets_block: dict[str, Any] = {str(i): "pending" for (i, _s, _rc) in scope}
    step_details: dict[str, Any] = {"sheets": sheets_block}
    report_lock = asyncio.Lock()
    done = 0  # asyncio single-thread → plain monotonic counter (no lock needed)

    async def detect_one(sheet_index: int, sheet: dict, result_crops: list[dict]) -> None:
        nonlocal done
        key = str(sheet_index)
        # Cooperative cancel boundary (pre-launch): a not-yet-started sheet skips
        # WITHOUT incrementing `done` (it never ran) — counts as cancelled.
        if await ctx.check_cancel():
            sheets_block[key] = "cancelled"
            return
        sheets_block[key] = "running"
        try:
            eligible_crops = _eligible_sheet_crops(sheet)
            sheet_objects = select_sheet_objects(object_map, sheet)
            req = build_detect_request(
                sheet, sheet_objects, eligible_crops, result_crops, controls
            )
            # Core self-throttles each ainvoke via its OWN _DETECT_SEM=3 (separate
            # flash pool) → no handler semaphore; fan-out all sheets at once.
            res = await run_detect_swap_defects(req, ai_context=ai_ctx)
            dims = res.meta.swappedDimensions
            defects_by_sheet.append(
                {
                    "sheet_index": sheet_index,
                    "defects": [d.model_dump(exclude_none=True) for d in res.defects],
                    "swappedDimensions": {"width": dims.width, "height": dims.height},
                    "defectCount": res.meta.defectCount,
                    "truncated": bool(res.meta.truncated),
                }
            )
            # PII-safe: counts only (NO message/url).
            sheets_block[key] = {"state": "done", "defect_count": res.meta.defectCount}
        except RemixDomainError as exc:
            _push_error(errors, _error_code(exc), sheet_index=sheet_index)
            sheets_block[key] = {"state": "failed", "code": _error_code(exc)}
        except Exception as exc:  # noqa: BLE001 — per-sheet isolation, non-fatal
            logger.exception(
                "detect_sheet_unexpected job_id=%s sheet_index=%d", ctx.id, sheet_index
            )
            _push_error(errors, "INTERNAL", sheet_index=sheet_index)
            sheets_block[key] = {"state": "failed", "code": "INTERNAL"}
        finally:
            done += 1
            async with report_lock:
                await ctx.report(current_step=done, step_details=step_details)

    # CONCURRENT fan-out — global _DETECT_SEM=3 bounds the in-flight flash calls.
    await asyncio.gather(*[detect_one(i, s, rc) for (i, s, rc) in scope])

    # Late cancel: keep inspected work when any sheet completed (mirror job 02 —
    # only 'cancelled' when nothing was inspected).
    if await ctx.check_cancel() and not defects_by_sheet:
        return (
            "cancelled",
            _build_result(defects_by_sheet, skipped_sheets, errors),
        )

    logger.info(
        "remix_detect_defects_done job_id=%s sheets=%d inspected=%d skipped=%d errors=%d",
        ctx.id, len(scope), len(defects_by_sheet), skipped_sheets, len(errors),
    )
    return (
        "completed",
        _build_result(defects_by_sheet, skipped_sheets, errors),
    )
