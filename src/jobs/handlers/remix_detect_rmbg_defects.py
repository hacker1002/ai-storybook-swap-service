"""Handler `remix_detect_rmbg_defects` — inspect every crop sheet of ONE rmbg
BATCH (`remixes.rmbgs[]`, identified by `id`) for remove-bg defects via the AI
core `run_detect_rmbg_defects` ([api/remix/08]), returning the located defect
regions per sheet through `background_jobs.result.defectsBySheet`.

RMBG-plane sibling of `remix_detect_defects` (job 11, sprite) +
`remix_detect_mix_defects` (job 12, mix). THE SIMPLEST resolve (spec 13): it loops
`rmbgs[batch].crop_sheets[]` and the body is built by the SHARED rmbg resolver
(`services/jobs/remix_rmbg_resolver.resolve_detect_rmbg_body`, reused with job 09)
— rmbg has NO target pool / lineup / annotation_map, so there is NO `swap_targets`
resolve and NO `MISSING_OBJECT_CONFIG` precondition. The core composes only 2
sheets (ORIGINAL still-bg + RESULT RGBA cut-out, no variant sheets) per sheet.

⚡ CONCURRENT per-sheet (parity job 11, NOT job 12's sequential loop — validated
Q2): the rmbg core is light (2 images, no variant sheets), so sheets fan out via
`asyncio.gather(..., return_exceptions=True)` bounded by a module-level
`_DETECT_SEM = asyncio.Semaphore(3)`. The core ALSO self-throttles each Gemini
ainvoke via its OWN `_DETECT_SEM=3` (separate flash pool); the handler sem bounds
the whole per-sheet pipeline (resolve + core call) at the SAME cap so in-flight
Gemini work never exceeds 3. `current_step` is a MONOTONIC completed-count
(incremented in `finally`), NOT a loop index — gather completion order is
non-deterministic.

Same job mechanics as job 11/12: NO persistence to `remixes` (defects are ADVISORY
/ ephemeral — this handler NEVER issues an `UPDATE remixes`); a per-sheet core
failure is NON-FATAL (recorded in `errors[]`, siblings continue); an empty
`defects` list is SUCCESS (inspected, found nothing wrong). The whole job is
`failed` only on an exception OUTSIDE the per-sheet loop (load remix / batch
not found). Cooperative cancel at the sheet boundary.

PII: never log `defect.message`, raw URLs, or human data — counts only. `result`
carries no signed URL (coords + labels only — safe for realtime).

`result` shape (frontend contract — spec 13 §Result; Phase 03 depends on it):
    {
      "defectsBySheet": [
        {
          "sheet_index": int,
          "defects": [ <RmbgDefect.model_dump(exclude_none=True)>, ... ],
          "swappedDimensions": {"width": int, "height": int},  # == sheet_geometry
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

from src.db.adapter import get_adapter
from src.jobs.runner import JobContext, register
from src.models.jobs.remix_detect_rmbg_defects import MAX_RESULT_ERRORS
from src.services.ai_usage import AiCallContext
from src.services.jobs.remix_rmbg_resolver import resolve_detect_rmbg_body
from src.services.remix.detect_rmbg_defects_core import run_detect_rmbg_defects
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import find_batch_by_id

# Reuse the generic "selected swap" gate helper from job 11 (a selected
# swap_result is a selected swap_result regardless of plane — rmbg stores the
# remove-bg RESULT in the SAME `swap_results[is_selected].{media_url, crops[]}`
# shape) — DRY, 1 source for the router precount + the handler scope.
from src.jobs.handlers.remix_detect_defects import (
    selected_swap_media_url,
    selected_swap_result,
)

logger = logging.getLogger(__name__)

__all__ = [
    "handle",
    "selected_swap_media_url",
    "selected_swap_result",
]


# CONCURRENT cap — fan out the per-sheet pipeline at 3 (parity job 11). The core's
# OWN `_DETECT_SEM=3` independently bounds in-flight Gemini-flash calls; this
# handler sem bounds the whole per-sheet pipeline at the SAME ceiling.
_DETECT_CONCURRENCY_CAP = 3
_DETECT_SEM = asyncio.Semaphore(_DETECT_CONCURRENCY_CAP)


# ─── result/error helpers (mirror job 11/12) ─────────────────────────────────


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


@register("remix_detect_rmbg_defects")
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

    # ── Load remix fresh — full row (rmbg resolve reads ONLY rmbgs[]). ──
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception:  # noqa: BLE001
        logger.exception("detect_rmbg_remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "INTERNAL")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    if not remix:
        _push_error(errors, "REMIX_NOT_FOUND")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    rmbgs: list = remix.get("rmbgs") or []
    batch = find_batch_by_id(rmbgs, batch_id)
    if batch is None:
        _push_error(errors, "BATCH_NOT_FOUND")
        return ("failed", _build_result(defects_by_sheet, 0, errors))

    # ── Scope = every sheet with a selected remove-bg result (the AFTER RGBA
    #    sheet to inspect). skipped_sheets = the rest (no selected rmbg). ──
    crop_sheets: list = batch.get("crop_sheets") or []
    eligible: list[tuple[int, dict]] = []
    for i, sheet in enumerate(crop_sheets):
        if not isinstance(sheet, dict):
            continue
        if selected_swap_result(sheet) is None:
            continue
        eligible.append((i, sheet))
    skipped_sheets = len(crop_sheets) - len(eligible)

    if not eligible:
        # Enqueue guards NO_RMBG_RESULT; defensive — nothing to inspect.
        return ("completed", _build_result(defects_by_sheet, skipped_sheets, errors))

    logger.info(
        "remix_detect_rmbg_defects_start job_id=%s remix_id=%s sheets=%d skipped=%d",
        ctx.id, remix_id, len(eligible), skipped_sheets,
    )

    # Cooperative cancel — pre-gather boundary (nothing inspected yet).
    if await ctx.check_cancel():
        return ("cancelled", _build_result(defects_by_sheet, skipped_sheets, errors))

    sheets_block: dict[str, Any] = {str(i): "pending" for (i, _s) in eligible}
    step_details: dict[str, Any] = {"sheets": sheets_block}
    report_lock = asyncio.Lock()
    done = 0  # asyncio single-thread → plain monotonic counter (no lock needed)

    async def detect_one(sheet_index: int, sheet: dict) -> None:
        nonlocal done
        key = str(sheet_index)
        async with _DETECT_SEM:
            # Cooperative cancel boundary (pre-launch): a not-yet-started sheet
            # skips WITHOUT incrementing `done` (it never ran) — counts as cancelled.
            if await ctx.check_cancel():
                sheets_block[key] = "cancelled"
                return
            sheets_block[key] = "running"
            try:
                selected = selected_swap_result(sheet)  # eligibility already proven
                body = resolve_detect_rmbg_body(sheet, selected or {}, controls)
                res = await run_detect_rmbg_defects(body, ai_context=ai_ctx)
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
            except Exception:  # noqa: BLE001 — per-sheet isolation, non-fatal
                logger.exception(
                    "detect_rmbg_sheet_unexpected job_id=%s sheet_index=%d", ctx.id, sheet_index
                )
                _push_error(errors, "INTERNAL", sheet_index=sheet_index)
                sheets_block[key] = {"state": "failed", "code": "INTERNAL"}
            finally:
                done += 1
                async with report_lock:
                    await ctx.report(current_step=done, step_details=step_details)

    # CONCURRENT fan-out — _DETECT_SEM=3 bounds the per-sheet pipeline; the core's
    # own _DETECT_SEM=3 independently bounds the in-flight flash calls.
    # return_exceptions=True is the safety net for anything escaping detect_one
    # (e.g. ctx.report raising) so one sheet can never abort the whole gather.
    await asyncio.gather(
        *[detect_one(i, s) for (i, s) in eligible], return_exceptions=True
    )

    # Late cancel: keep inspected work when any sheet completed (parity job 11/12)
    # — only 'cancelled' when nothing was inspected.
    if await ctx.check_cancel() and not defects_by_sheet:
        return ("cancelled", _build_result(defects_by_sheet, skipped_sheets, errors))

    logger.info(
        "remix_detect_rmbg_defects_done job_id=%s inspected=%d skipped=%d errors=%d",
        ctx.id, len(defects_by_sheet), skipped_sheets, len(errors),
    )
    return ("completed", _build_result(defects_by_sheet, skipped_sheets, errors))
