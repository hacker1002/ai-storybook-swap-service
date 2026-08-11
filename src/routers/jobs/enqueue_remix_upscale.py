"""POST /api/jobs/remix/{remix_id}/upscale — enqueue remix upscale job (10).

Ported from image-api. Thin wrapper over `enqueue_pipeline_stage_job`. Resolves
the `upscale` model group at the ROUTER (recraft/alexgenovese/xinntao/real-esrgan
all dispatch; unknown → 422 UNSUPPORTED_MODEL) + normalizes the top-level
model-agnostic `grain` into `params.grain`. Auth = editor-session Bearer (router
group); `session` supplies audit `admin_ref`/`sid`.

Returns 201 enqueued | 200 skipped | 200 dedup.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_UPSCALE
from src.jobs.model_registry import resolve_model_params
from src.models.jobs.remix_upscale import (
    RemixUpscaleEnqueueRequest,
    normalize_grain,
)
from src.routers.jobs.pipeline_stage_enqueue import enqueue_pipeline_stage_job

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-crop Replicate upscale ≈ 10-20s, sequential → estimate by TOTAL crop count.
_ESTIMATED_SEC_PER_CROP = 15


def _estimate(batch: dict, in_scope: list[int]) -> int:
    sheets = batch.get("crop_sheets") or []
    total_crops = 0
    for i in in_scope:
        if 0 <= i < len(sheets) and isinstance(sheets[i], dict):
            total_crops += len(sheets[i].get("original_crops") or [])
    return max(total_crops, len(in_scope)) * _ESTIMATED_SEC_PER_CROP


@router.post("/remix/{remix_id}/upscale")
async def enqueue_remix_upscale_endpoint(
    remix_id: str,
    body: RemixUpscaleEnqueueRequest,
    session: EditorSessionContext = Depends(require_editor_session),
):
    model_params = resolve_model_params(
        body.model_params.model_dump() if body.model_params else None, "upscale"
    )
    logger.info("upscale_model_resolved remix_id=%s model=%s", remix_id, model_params["model"])
    # Grain is top-level + model-agnostic → normalize here, persist into params.grain.
    grain = normalize_grain(body.grain)
    extra_params = {"grain": grain} if grain is not None else None
    return await enqueue_pipeline_stage_job(
        session=session,
        table="remixes",
        row_id=remix_id,
        id_key="remix_id",
        not_found_code="REMIX_NOT_FOUND",
        batch_id=body.batch_id,
        force_resweep=body.force_resweep,
        stage_column="upscales",
        job_type=JOB_TYPE_UPSCALE,
        estimate_duration_sec=_estimate,
        model_params=model_params,
        extra_params=extra_params,
    )
