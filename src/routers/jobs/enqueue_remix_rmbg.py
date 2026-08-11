"""POST /api/jobs/remix/{remix_id}/rmbg — enqueue remix remove-bg job (09).

Ported from image-api. Thin wrapper over `enqueue_pipeline_stage_job` (rmbg +
upscale share it). Resolves the `rmbg` model group at the ROUTER (→ 422
UNSUPPORTED_MODEL before any DB work); omit → bria default. Auth is the
editor-session Bearer at the router-group level; the `session` dep supplies the
audit `admin_ref`/`sid` stamped into the job params.

Returns 201 enqueued | 200 skipped | 200 dedup.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_RMBG
from src.jobs.model_registry import resolve_model_params
from src.models.jobs.remix_rmbg import RemixRmbgEnqueueRequest
from src.routers.jobs.pipeline_stage_enqueue import enqueue_pipeline_stage_job

logger = logging.getLogger(__name__)

router = APIRouter()

# compose ~2s + remove-bg ~5-10s (Replicate) + cut/upload ~2s per sheet.
_ESTIMATED_SEC_PER_SHEET = 15


@router.post("/remix/{remix_id}/rmbg")
async def enqueue_remix_rmbg_endpoint(
    remix_id: str,
    body: RemixRmbgEnqueueRequest,
    session: EditorSessionContext = Depends(require_editor_session),
):
    model_params = resolve_model_params(
        body.model_params.model_dump() if body.model_params else None, "rmbg"
    )
    logger.info("rmbg_model_resolved remix_id=%s model=%s", remix_id, model_params["model"])
    return await enqueue_pipeline_stage_job(
        session=session,
        table="remixes",
        row_id=remix_id,
        id_key="remix_id",
        not_found_code="REMIX_NOT_FOUND",
        batch_id=body.batch_id,
        force_resweep=body.force_resweep,
        stage_column="rmbgs",
        job_type=JOB_TYPE_RMBG,
        estimate_duration_sec=lambda _batch, in_scope: (
            len(in_scope) * _ESTIMATED_SEC_PER_SHEET
        ),
        model_params=model_params,
    )
