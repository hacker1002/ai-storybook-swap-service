"""POST /api/remix/swap-mix-crop-sheet — router wrapper.

NO FE consumer today — internal/test only (job 04/… calls the core in-process).

Thin transport layer: Pydantic body → call `run_swap_mix_sheet()` → build response
envelope with multi-target observability meta. `RemixDomainError` bubbles through to
the app-level handler (`error_handler.py`, registered in `main.py`). Auth =
editor-session Bearer at the router group level (`router.py`).

The opt-in `saveResource` auto-persist (`resource_persist`) is wired VERBATIM from
image-api so the response shape stays byte-identical (the save-* fields ride on
`data`). In THIS editor-facing service `save_generated_resource` is a documented
NO-OP (see `src/services/resource_persist/`), so with no `save_resource` sent the
outcome is None and the fields render null — same keys as image-api.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

from src.models.requests.swap_mix_crop_sheet import (
    MixGeminiPayloadBytesMeta,
    MixSheetDimensionsMeta,
    MixSkippedReferenceMeta,
    SwapMixCropSheetMeta,
    SwapMixCropSheetResponse,
    SwapMixSheetCoreRequest,
    SwapMixSheetCoreResultData,
    VariantSheetUrls,
)
from src.jobs.model_registry import SWAP_DEFAULT_MODEL, resolve_model_params
from src.routers._shared.deps import error_response
from src.services.ai_usage import AiCallContext
from src.services.remix.errors import RemixDomainError
from src.services.remix.swap_mix_sheet_core import run_swap_mix_sheet
from src.services.resource_persist import (
    GeneratedResourceValue,
    PersistContext,
    save_generated_resource,
    save_response_fields,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/swap-mix-crop-sheet",
    response_model=SwapMixCropSheetResponse,
)
async def swap_mix_crop_sheet(
    body: SwapMixSheetCoreRequest,
) -> SwapMixCropSheetResponse:
    t0 = time.monotonic()
    n_crops = len(body.crops)
    n_targets = len(body.swap_targets)
    logger.info(
        "swap_mix_start n_crops=%d n_targets=%d return_composed=%s",
        n_crops, n_targets, body.return_composed_sheet,
    )

    # fix #1 [SECURITY]: validate the client-supplied model through the swap
    # allowlist registry BEFORE dispatch (parity with the job enqueue path,
    # ADR-038/049). An unknown model → 422 UNSUPPORTED_MODEL (raised here, bubbles
    # to the app-level envelope handler); omit → default. Closes the sync-swap
    # allowlist bypass where a junk model silently fell back to the default (+ was
    # billed). Forward the normalized public model + clamped temperature into the
    # core request — dispatch id stays byte-identical for the omit/valid paths.
    normalized = resolve_model_params(
        {"model": body.model or SWAP_DEFAULT_MODEL, "params": {"temperature": body.temperature}},
        "swap",
    )
    body = body.model_copy(
        update={"model": normalized["model"], "temperature": normalized["params"]["temperature"]}
    )

    try:
        # AI-usage attribution (Phase 05): optional `remixId` → remix cost bucket
        # (discriminator). None → unattributed (remix billing also flows via jobs 04/…).
        result = await run_swap_mix_sheet(
            body, ai_context=AiCallContext(remix_id=body.remixId)
        )
    except RemixDomainError:
        raise
    except Exception as exc:
        logger.exception("swap_mix_unhandled")
        raise error_response(
            500, "INTERNAL_ERROR", "Unexpected error during mix swap"
        ) from exc

    processing_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "swap_mix_done processing_ms=%d compose_ms=%d gemini_ms=%d upload_ms=%d targets=%d with_base=%d tokens=%s",
        processing_ms, result.compose_ms, result.gemini_ms, result.upload_ms,
        result.target_count, result.targets_with_base, result.token_usage,
    )


    # Opt-in auto-persist — remix image edit. NO-OP in this service unless a
    # `saveResource` directive is sent (parity seam with image-api).
    save_outcome = await save_generated_resource(
        body.save_resource,
        GeneratedResourceValue(
            media_url=result.image_url,
            ai_request_id=result.ai_request_id,
        ),
        PersistContext(remix_id=body.remixId),
    )

    return SwapMixCropSheetResponse(
        success=True,
        data=SwapMixSheetCoreResultData(
            image_url=result.image_url,
            width=result.width,
            height=result.height,
            token_usage=result.token_usage,
            composed_sheet_url=result.composed_sheet_url,
            variant_sheet_urls=(
                VariantSheetUrls(**result.variant_sheet_urls)
                if result.variant_sheet_urls
                else None
            ),
            aiRequestId=result.ai_request_id,
            **save_response_fields(save_outcome),
        ),
        meta=SwapMixCropSheetMeta(
            processingTime=processing_ms,
            composeMs=result.compose_ms,
            geminiMs=result.gemini_ms,
            uploadMs=result.upload_ms,
            tokenUsage=result.token_usage,
            sheetDimensions=MixSheetDimensionsMeta(
                width=result.width, height=result.height,
            ),
            geminiPayloadBytes=MixGeminiPayloadBytesMeta(
                sheet=result.payload_bytes_sheet,
                variant_old=result.payload_bytes_variant_old,
                variant_new=result.payload_bytes_variant_new,
            ),
            targetCount=result.target_count,
            targetsWithBase=result.targets_with_base,
            skippedReferences=[
                MixSkippedReferenceMeta(
                    kind=s["kind"],
                    target_key=s.get("target_key"),
                    reason=s["reason"],
                )
                for s in result.skipped_references
            ]
            or None,
        ),
    )
