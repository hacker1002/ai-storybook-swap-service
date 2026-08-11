"""POST /api/image/upscale-image handler (P3c port — thin HTTP wrapper).

Ported from `ai-storybook-image-api/src/routers/image/upscale_image.py`. Maps
`UpscaleImageParams` → `UpscaleCoreRequest` → `run_upscale` → spec envelope. The
upscale CORE (`run_upscale`, per-model adapters, tile mode) was already ported in
P3b for the `remix_upscale` job — this route reuses it verbatim (DRY, no second copy).

AUTH DELTA vs image-api: editor-session Bearer at the router group level (NOT
X-API-Key). The handler reads the session ctx for AI-usage audit.

ENVELOPE: `ImageDomainError` is NOT caught here — it bubbles to the app-level
`@app.exception_handler(ImageDomainError)` in `main.py` (same handler that catches
validator-raised INVALID_IMAGE_SOURCE during body parsing). `RemixDomainError`
(UNSUPPORTED_MODEL) is remapped to `ImageDomainError` for a consistent spec
envelope. Only unexpected exceptions map to 500 INTERNAL_ERROR.
"""

import logging
import time

from fastapi import APIRouter, Depends

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.jobs.model_registry import resolve_model_params
from src.models.requests.upscale_image import (
    GrainMeta,
    UpscaleCoreRequest,
    UpscaleImageData,
    UpscaleImageMeta,
    UpscaleImageParams,
    UpscaleImageResponse,
)
from src.routers._shared.deps import error_response
from src.services.ai_usage import AiCallContext
from src.services.image.errors import ImageDomainError
from src.services.image.upscale_core import run_upscale
from src.services.remix.errors import RemixDomainError
from src.services.resource_persist import (
    GeneratedResourceValue,
    PersistContext,
    save_generated_resource,
    save_response_fields,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/upscale-image", response_model=UpscaleImageResponse)
async def upscale_image(
    params: UpscaleImageParams,
    session: EditorSessionContext = Depends(require_editor_session),
) -> UpscaleImageResponse:
    t0 = time.monotonic()
    source_type = "url" if params.imageUrl else "base64"

    # Resolve the upscaler model via the shared allowlist (parity job 10). Only the
    # model id is taken from resolve — face_enhance is read directly from
    # `modelParams.params` below. `RemixDomainError(422, UNSUPPORTED_MODEL)` →
    # `ImageDomainError` for a consistent spec envelope (app handler renders it).
    mp = params.modelParams
    model_dict = (
        {"model": mp.model, "params": {}} if (mp is not None and mp.model) else None
    )
    try:
        resolved = resolve_model_params(model_dict, "upscale")
    except RemixDomainError as exc:
        raise ImageDomainError(
            status=exc.status, code=exc.code, message=exc.message, details=exc.details
        ) from exc
    model_id = resolved["model"]

    # faceEnhance (1 bool): modelParams.params.faceEnhance, default True. recraft
    # ignores it; real-esrgan/alexgenovese honor it via their adapter.
    mp_face = (
        mp.params.faceEnhance
        if (mp is not None and mp.params is not None and mp.params.faceEnhance is not None)
        else None
    )
    face_enhance = mp_face if mp_face is not None else True

    logger.info(
        "upscale_image_start source_type=%s scale=%s face=%s model=%s",
        source_type, params.scale, face_enhance, model_id,
    )

    core_req = UpscaleCoreRequest(
        imageUrl=str(params.imageUrl) if params.imageUrl else None,
        imageBase64=params.imageBase64,
        scale=params.scale,
        faceEnhance=face_enhance,
        model=model_id,
        originName=None,  # core derives from URL filename; base64 → "image"
        grain=params.grain,  # top-level, model-agnostic; None → core skips grain
    )

    # AI-usage attribution — DUAL context (EditImageModal book+remix). Stamp ONLY the
    # winner: `remixId` ⇒ remix cost (snapshot_id NULL), else `snapshotId` ⇒ book cost.
    # admin_ref/sid from the editor session ride into `request.audit`.
    if params.remixId:
        ai_ctx = AiCallContext(
            remix_id=params.remixId, admin_ref=session.admin_ref, sid=session.sid
        )
    else:
        ai_ctx = AiCallContext(
            snapshot_id=params.snapshotId, admin_ref=session.admin_ref, sid=session.sid
        )

    try:
        # aiRequestId is nullable: the tiled path returns None (N predictions, no
        # single id) — mapped verbatim below (never coerced to "").
        result = await run_upscale(core_req, ai_context=ai_ctx)
    except ImageDomainError:
        raise  # → app-level handler (spec envelope at exc.status)
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        logger.error("upscale_image_unexpected source_type=%s err=%s", source_type, exc)
        raise error_response(500, "INTERNAL_ERROR", "Unexpected error") from exc

    # Opt-in auto-persist (no-op parity seam). Base64 source → no pre-edit URL.
    save_outcome = await save_generated_resource(
        params.saveResource,
        GeneratedResourceValue(
            media_url=result.imageUrl or "",
            storage_path=result.storagePath,
            ai_request_id=result.ai_request_id,
            original_url=str(params.imageUrl) if params.imageUrl else None,
        ),
        PersistContext(snapshot_id=params.snapshotId, remix_id=params.remixId),
    )

    processing_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "upscale_image_ok source_type=%s scale=%s w=%d h=%d processing_ms=%d",
        result.sourceType, result.scale, result.width, result.height, processing_ms,
    )

    return UpscaleImageResponse(
        success=True,
        data=UpscaleImageData(
            imageUrl=result.imageUrl,
            storagePath=result.storagePath,
            width=result.width,
            height=result.height,
            aiRequestId=result.ai_request_id,  # verbatim — None on the tiled path
            **save_response_fields(save_outcome),
        ),
        meta=UpscaleImageMeta(
            processingTime=processing_ms,
            mimeType=result.mimeType,
            scale=result.scale,
            sourceType=result.sourceType,
            tileCount=result.tileCount,
            replicatePredictionIds=result.replicatePredictionIds or None,
            model=model_id,
            fixedRatio=result.fixedRatio,
            variant=result.variant,
            grainApplied=result.grainApplied,
            grain=(
                GrainMeta(
                    amp=result.grain.amp,
                    blur=result.grain.blur,
                    seed=result.grain.seed,
                )
                if (result.grainApplied and result.grain is not None)
                else None
            ),
        ),
    )
