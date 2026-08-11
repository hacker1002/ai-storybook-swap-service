"""POST /api/jobs/remix/{remix_id}/detect-rmbg-defects — enqueue RMBG defect job (13).

Ported from image-api with the P3b seam swaps (`get_adapter`, ownership DROPPED,
errors → `RemixDomainError`, audit stamped). THE SIMPLEST detect enqueue — resolve
reads ONLY `rmbgs[]`; NO target pool / lineup / snapshot → NO MISSING_OBJECT_CONFIG.
Defects ADVISORY.

Dedup → **409 JOB_ALREADY_ACTIVE** (matches job 12). Independent of rmbg-swap (09) +
detect-mix (12) + detect-sprite (11). Precondition NO_RMBG_RESULT → 422 (not 200).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_DETECT_RMBG
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.jobs.handlers.remix_detect_rmbg_defects import selected_swap_media_url
from src.models.jobs.remix_detect_rmbg_defects import RemixDetectRmbgDefectsEnqueueRequest
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import find_batch_by_id

logger = logging.getLogger(__name__)

router = APIRouter()

# ORIGINAL + RESULT compose only (no variant sheets) → lighter than job 12.
_ESTIMATED_SEC_PER_SHEET = 8


@router.post("/remix/{remix_id}/detect-rmbg-defects")
async def enqueue_remix_detect_rmbg_defects_endpoint(
    remix_id: str,
    body: RemixDetectRmbgDefectsEnqueueRequest,
    session: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()
    batch_id = body.batch_id

    # 1. Load remix (rmbgs column + snapshot_id).
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="remix lookup failed") from exc

    if not remix:
        raise RemixDomainError(status=404, code="REMIX_NOT_FOUND", message=f"remix {remix_id} not found")

    rmbgs = remix.get("rmbgs") or []
    snapshot_id = remix.get("snapshot_id")
    if not snapshot_id:
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="remix is missing snapshot_id")

    # 2. Resolve the batch entry by id.
    batch = find_batch_by_id(rmbgs, batch_id)
    if not batch:
        raise RemixDomainError(status=404, code="BATCH_NOT_FOUND", message=f"batch {batch_id} not found")

    # 3. book_id via bridge (ownership DROPPED; rmbg has no target pool).
    try:
        book_id = await adapter.get_book_id_for_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("book_lookup_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="book lookup failed") from exc

    # 4. Scope = every sheet with a selected remove-bg result. None → 422 NO_RMBG_RESULT.
    crop_sheets = batch.get("crop_sheets") or []
    sheets_to_process = [
        i
        for i, sheet in enumerate(crop_sheets)
        if isinstance(sheet, dict) and selected_swap_media_url(sheet)
    ]
    if not sheets_to_process:
        raise RemixDomainError(status=422, code="NO_RMBG_RESULT", message="batch has no remove-bg result sheet to inspect")

    # 5. Dedup — any active detect-rmbg job for this remix → 409 (returns existing).
    try:
        existing = await adapter.find_active_job(remix_id, JOB_TYPE_DETECT_RMBG)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dedup_check_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="dedup lookup failed") from exc

    if existing:
        raise RemixDomainError(
            status=409,
            code="JOB_ALREADY_ACTIVE",
            message="a detect-rmbg job is already active for this remix",
            details={
                "job_id": str(existing["id"]),
                "status": existing["status"],
                "type": existing.get("type"),
                "remix_id": remix_id,
                "batch_id": (existing.get("params") or {}).get("batch_id"),
            },
        )

    # 6. Enqueue via jobs lib.
    try:
        job = await enqueue(
            type=JOB_TYPE_DETECT_RMBG,
            book_id=book_id,
            params={
                "remix_id": remix_id,
                "batch_id": batch_id,
                "force_resweep": body.force_resweep,
                "snapshot_id": snapshot_id,
                "admin_ref": session.admin_ref,
                "sid": session.sid,
                "controls": {
                    "severity_threshold": body.severity_threshold,
                    "max_defects": body.max_defects,
                },
            },
            total_steps=len(sheets_to_process),
        )
    except ValueError as exc:
        raise RemixDomainError(status=500, code="JOB_INSERT_FAILED", message=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("enqueue_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="JOB_INSERT_FAILED", message=str(exc)) from exc

    # 7. Init step_details (1 extra UPDATE; handler has a defensive rebuild).
    step_details = {"sheets": {str(i): "pending" for i in sheets_to_process}}
    try:
        await adapter.update_job(job["id"], {"step_details": step_details})
    except Exception as exc:  # noqa: BLE001
        logger.warning("step_details_init_failed job_id=%s msg=%s", job["id"], exc)

    n = len(sheets_to_process)
    estimated = n * _ESTIMATED_SEC_PER_SHEET

    audit(session, endpoint="jobs.remix_detect_rmbg_defects", resource_id=remix_id, job_id=str(job["id"]), batch_id=batch_id, sheets=n)
    logger.info(
        "remix_detect_rmbg_defects_enqueued job_id=%s remix_id=%s sheets=%d",
        job["id"], remix_id, n,
    )

    # 8. Return 201.
    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": JOB_TYPE_DETECT_RMBG,
                "remix_id": remix_id,
                "batch_id": batch_id,
                "total_steps": n,
                "sheets_to_process": n,
                "estimated_duration_sec": estimated,
            },
        },
    )
