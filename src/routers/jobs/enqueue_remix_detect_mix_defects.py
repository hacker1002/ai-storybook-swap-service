"""POST /api/jobs/remix/{remix_id}/detect-mix-defects — enqueue MIX defect job (12).

Ported from image-api with the P3b seam swaps (snapshot by id via `get_adapter`,
ownership DROPPED, errors → `RemixDomainError`, audit stamped). Defects ADVISORY.

⚠️ DIVERGENCE (kept verbatim): dedup → **409 JOB_ALREADY_ACTIVE** (unlike sprite
detect job 11 which returns 200). Independent of mix-swap (05) + sprite-detect (11).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_DETECT_MIX
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.jobs.handlers.remix_detect_mix_defects import selected_swap_media_url
from src.models.jobs.remix_detect_mix_defects import RemixDetectMixDefectsEnqueueRequest
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import (
    batch_lineup_tokens,
    find_batch_by_id,
    resolve_mix_swap_context,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ESTIMATED_SEC_PER_SHEET = 12


@router.post("/remix/{remix_id}/detect-mix-defects")
async def enqueue_remix_detect_mix_defects_endpoint(
    remix_id: str,
    body: RemixDetectMixDefectsEnqueueRequest,
    session: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()
    batch_id = body.batch_id

    # 1. Load remix.
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="remix lookup failed") from exc

    if not remix:
        raise RemixDomainError(status=404, code="REMIX_NOT_FOUND", message=f"remix {remix_id} not found")

    remix_characters = remix.get("characters") or []
    remix_props = remix.get("props") or []
    remix_sprites = remix.get("sprites") or []
    mixes = remix.get("mixes") or []
    snapshot_id = remix.get("snapshot_id")
    if not snapshot_id:
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="remix is missing snapshot_id")

    # 2. Resolve the batch entry by id.
    batch = find_batch_by_id(mixes, batch_id)
    if not batch:
        raise RemixDomainError(status=404, code="BATCH_NOT_FOUND", message=f"batch {batch_id} not found")

    # 3. snapshot characters/props (ownership DROPPED). book_id via bridge.
    try:
        snap = await adapter.get_snapshot(snapshot_id)
        snap_characters = (snap or {}).get("characters") or []
        snap_props = (snap or {}).get("props") or []
        book_id = await adapter.get_book_id_for_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("snapshot_lookup_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="snapshot lookup failed") from exc

    # 4. Scope = every sheet with a selected swap. None → 422 NO_SWAP_RESULT.
    crop_sheets = batch.get("crop_sheets") or []
    sheets_to_process = [
        i
        for i, sheet in enumerate(crop_sheets)
        if isinstance(sheet, dict) and selected_swap_media_url(sheet)
    ]
    if not sheets_to_process:
        raise RemixDomainError(status=422, code="NO_SWAP_RESULT", message="batch has no swapped crop sheet to inspect")

    # 5. Resolve the shared target pool (1× — lineup constant).
    ctx = resolve_mix_swap_context(
        batch,
        remix_characters,
        remix_props,
        snap_characters,
        snap_props,
        remix_sprites=remix_sprites,
    )
    if not ctx.swap_targets:
        raise RemixDomainError(
            status=422,
            code="MISSING_OBJECT_CONFIG",
            message="batch has no token resolvable to a swap target",
        )

    # 6. focus_objects must be a subset of the BATCH lineup → 400 VALIDATION_ERROR.
    if body.focus_objects is not None:
        valid = set(batch_lineup_tokens(batch))
        unknown = sorted({k for k in body.focus_objects if k not in valid})
        if unknown:
            raise RemixDomainError(
                status=400,
                code="VALIDATION_ERROR",
                message="focus_objects must be a subset of the batch lineup",
                details={"focus_objects": unknown},
            )

    # 7. Dedup — any active detect-mix job for this remix → 409 (returns existing).
    try:
        existing = await adapter.find_active_job(remix_id, JOB_TYPE_DETECT_MIX)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dedup_check_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="dedup lookup failed") from exc

    if existing:
        raise RemixDomainError(
            status=409,
            code="JOB_ALREADY_ACTIVE",
            message="a detect-mix job is already active for this remix",
            details={
                "job_id": str(existing["id"]),
                "status": existing["status"],
                "type": existing.get("type"),
                "remix_id": remix_id,
                "batch_id": (existing.get("params") or {}).get("batch_id"),
            },
        )

    # 8. Enqueue via jobs lib.
    try:
        job = await enqueue(
            type=JOB_TYPE_DETECT_MIX,
            book_id=book_id,
            params={
                "remix_id": remix_id,
                "batch_id": batch_id,
                "force_resweep": body.force_resweep,
                "snapshot_id": snapshot_id,
                "admin_ref": session.admin_ref,
                "sid": session.sid,
                "controls": {
                    "swap_model": body.swap_model,
                    "swap_temperature": body.swap_temperature,
                    "focus_objects": body.focus_objects,
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

    # 9. Init step_details (1 extra UPDATE; handler has a defensive rebuild).
    step_details = {"sheets": {str(i): "pending" for i in sheets_to_process}}
    try:
        await adapter.update_job(job["id"], {"step_details": step_details})
    except Exception as exc:  # noqa: BLE001
        logger.warning("step_details_init_failed job_id=%s msg=%s", job["id"], exc)

    n = len(sheets_to_process)
    estimated = n * _ESTIMATED_SEC_PER_SHEET

    audit(session, endpoint="jobs.remix_detect_mix_defects", resource_id=remix_id, job_id=str(job["id"]), batch_id=batch_id, sheets=n)
    logger.info(
        "remix_detect_mix_defects_enqueued job_id=%s remix_id=%s sheets=%d targets=%d",
        job["id"], remix_id, n, ctx.target_count,
    )

    # 10. Return 201.
    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": JOB_TYPE_DETECT_MIX,
                "remix_id": remix_id,
                "batch_id": batch_id,
                "target_count": ctx.target_count,
                "total_steps": n,
                "sheets_to_process": n,
                "estimated_duration_sec": estimated,
            },
        },
    )
