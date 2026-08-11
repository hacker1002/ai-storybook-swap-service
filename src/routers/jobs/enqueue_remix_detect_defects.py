"""POST /api/jobs/remix/{remix_id}/detect-sprite-defects — enqueue sprite defect job (11).

Ported from image-api with the P3b seam swaps: `get_adapter()` (snapshot by id +
global humans read filtered by referenced ids), ownership DROPPED (existence check
only; book_id via the bridge), errors → `RemixDomainError`, audit `admin_ref`/`sid`
stamped into params. Defects are ADVISORY (handler writes `background_jobs.result`,
never `remixes`).

Dedup: strictly one active detect job per remix → **200 deduped** (independent of
sprite-swap + detect-mix/-rmbg; distinct `type`).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_DETECT_DEFECTS
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.jobs.handlers.remix_detect_defects import selected_swap_media_url
from src.models.jobs.remix_detect_defects import RemixDetectDefectsEnqueueRequest
from src.services.remix.errors import RemixDomainError
from src.services.remix.sprite_swap_resolver import (
    find_sprite_by_id,
    resolve_sprite_object_map,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ESTIMATED_SEC_PER_SHEET = 12
_DETECT_CONCURRENCY = 3


@router.post("/remix/{remix_id}/detect-sprite-defects")
async def enqueue_remix_detect_defects_endpoint(
    remix_id: str,
    body: RemixDetectDefectsEnqueueRequest,
    session: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()
    sprite_id = body.sprite_id

    # 1. Load remix.
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="remix lookup failed") from exc

    if not remix:
        raise RemixDomainError(status=404, code="REMIX_NOT_FOUND", message=f"remix {remix_id} not found")

    remix_config = remix.get("remix_config") or {}
    sprites = remix.get("sprites") or []
    snapshot_id = remix.get("snapshot_id")
    if not snapshot_id:
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="remix is missing snapshot_id")

    # 2. Resolve the sprite entry by id.
    sprite = find_sprite_by_id(sprites, sprite_id)
    if not sprite:
        raise RemixDomainError(status=404, code="SPRITE_NOT_FOUND", message=f"sprite {sprite_id} not found")

    # 3. snapshot characters (ownership DROPPED). book_id via bridge (may be None).
    try:
        snap = await adapter.get_snapshot(snapshot_id)
        snap_characters = (snap or {}).get("characters") or []
        book_id = await adapter.get_book_id_for_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("snapshot_lookup_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="snapshot lookup failed") from exc

    # 4. Resolve object pool (1× — lineup constant). Read referenced humans globally.
    rc_chars = remix_config.get("characters") or []
    human_ids = sorted(
        {c["human_id"] for c in rc_chars if isinstance(c, dict) and c.get("human_id")}
    )
    humans_by_id: dict[str, dict] = {}
    if human_ids:
        try:
            wanted = {str(h) for h in human_ids}
            for row in await adapter.list_humans(book_id):
                # humans.id is uuid.UUID (no pool text-codec); human_id is str →
                # coerce before matching, else humans_by_id stays empty and the
                # sprite objects wrongly fail the MISSING_OBJECT_CONFIG check.
                if isinstance(row, dict) and str(row.get("id")) in wanted:
                    humans_by_id[str(row["id"])] = row
        except Exception as exc:  # noqa: BLE001
            logger.exception("humans_load_failed remix_id=%s", remix_id)
            raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="humans lookup failed") from exc

    pool = resolve_sprite_object_map(sprite, remix_config, humans_by_id, snap_characters)

    # Precondition fail-loud (lineup constant → resolve once at enqueue).
    if not pool.lineup:
        raise RemixDomainError(status=422, code="NO_SWAP_OBJECTS", message="sprite has no character cell")
    if pool.missing:
        raise RemixDomainError(
            status=422,
            code="MISSING_OBJECT_CONFIG",
            message="one or more sprite objects are missing remix_config",
            details={"object_keys": list(dict.fromkeys(pool.missing))},
        )

    # 5. focus_objects must be a subset of the sprite lineup → 400 VALIDATION_ERROR.
    if body.focus_objects is not None:
        valid = set(pool.lineup)
        unknown = sorted({k for k in body.focus_objects if k not in valid})
        if unknown:
            raise RemixDomainError(
                status=400,
                code="VALIDATION_ERROR",
                message="focus_objects must be a subset of the sprite lineup",
                details={"focus_objects": unknown},
            )

    # 6. Scope = every sheet with a selected swap. None → 422 NO_SWAP_RESULT.
    crop_sheets = sprite.get("crop_sheets") or []
    sheets_to_process = [
        i
        for i, sheet in enumerate(crop_sheets)
        if isinstance(sheet, dict) and selected_swap_media_url(sheet)
    ]
    if not sheets_to_process:
        raise RemixDomainError(status=422, code="NO_SWAP_RESULT", message="sprite has no swapped crop sheet to inspect")

    # 7. Dedup — any active detect job for this remix → 200 deduped.
    try:
        existing = await adapter.find_active_job(remix_id, JOB_TYPE_DETECT_DEFECTS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dedup_check_failed remix_id=%s", remix_id)
        raise RemixDomainError(status=500, code="INTERNAL_ERROR", message="dedup lookup failed") from exc

    if existing:
        return {
            "success": True,
            "data": {
                "deduped": True,
                "job_id": str(existing["id"]),
                "status": existing["status"],
                "type": existing.get("type"),
                "remix_id": remix_id,
                "active_swap_key": (existing.get("params") or {}).get("sprite_id"),
            },
        }

    # 8. Enqueue via jobs lib.
    try:
        job = await enqueue(
            type=JOB_TYPE_DETECT_DEFECTS,
            book_id=book_id,
            params={
                "remix_id": remix_id,
                "sprite_id": sprite_id,
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
    estimated = -(-n // _DETECT_CONCURRENCY) * _ESTIMATED_SEC_PER_SHEET

    audit(session, endpoint="jobs.remix_detect_defects", resource_id=remix_id, job_id=str(job["id"]), sprite_id=sprite_id, sheets=n)
    logger.info(
        "remix_detect_defects_enqueued job_id=%s remix_id=%s sheets=%d objects=%d",
        job["id"], remix_id, n, pool.object_count,
    )

    # 10. Return 201.
    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": JOB_TYPE_DETECT_DEFECTS,
                "remix_id": remix_id,
                "sprite_id": sprite_id,
                "object_count": pool.object_count,
                "total_steps": n,
                "sheets_to_process": n,
                "estimated_duration_sec": estimated,
            },
        },
    )
