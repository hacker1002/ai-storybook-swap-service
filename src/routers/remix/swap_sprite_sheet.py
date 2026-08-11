"""POST /api/remix/swap-sprite-sheet — router wrapper.

Thin transport layer: auth → Pydantic body → call `run_swap_sprite_sheet()` →
build response envelope with per-object observability meta. `RemixDomainError`
bubbles through to the app-level handler in `main.py` (uniform JSON envelope).

`return_bytes` is a core-only field (in-process job 02 cut) — the public HTTP
body NEVER sets it (forced False here regardless of what the body says).

NO FE consumer today — internal/test/debug only (the job pipeline calls the
`run_*` cores in-process). Auth is the editor-session Bearer dep enforced at the
router-group level (`src/routers/remix/router.py`), NOT `X-API-Key`.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.models.requests.swap_sprite_sheet import (
    SpriteGeminiPayloadBytesMeta,
    SpriteSheetDimensionsMeta,
    SwapSpriteSheetCoreRequest,
    SwapSpriteSheetCoreResultData,
    SwapSpriteSheetMeta,
    SwapSpriteSheetResponse,
)
from src.jobs.model_registry import SWAP_DEFAULT_MODEL, resolve_model_params
from src.routers._shared.deps import error_response
from src.services.ai_usage import AiCallContext
from src.services.remix.errors import RemixDomainError
from src.services.remix.swap_sprite_sheet_core import run_swap_sprite_sheet

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/swap-sprite-sheet",
    response_model=SwapSpriteSheetResponse,
)
async def swap_sprite_sheet(
    body: SwapSpriteSheetCoreRequest,
    session: EditorSessionContext = Depends(require_editor_session),
) -> SwapSpriteSheetResponse:
    t0 = time.monotonic()
    # Force return_bytes False on the public body — it is an in-process-only seam
    # (job 02 cut). A stale/malicious caller cannot trigger raw-bytes mode.
    body.return_bytes = False

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

    n_crops = len(body.crops)
    n_objects = len(body.swap_objects)
    logger.info(
        "swap_sprite_start n_crops=%d n_objects=%d return_composed=%s",
        n_crops, n_objects, body.return_composed_sheet,
    )

    try:
        # AI-usage attribution (Phase 05): optional `remixId` → remix cost bucket
        # (discriminator). admin_ref/sid from the editor session ride into
        # `request.audit` (spec 08 §Delta — every ported endpoint stamps them).
        result = await run_swap_sprite_sheet(
            body,
            ai_context=AiCallContext(
                remix_id=body.remixId,
                admin_ref=session.admin_ref,
                sid=session.sid,
            ),
        )
    except RemixDomainError:
        raise
    except Exception as exc:
        logger.exception("swap_sprite_unhandled")
        raise error_response(
            500, "INTERNAL_ERROR", "Unexpected error during sprite swap"
        ) from exc

    processing_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "swap_sprite_done processing_ms=%d compose_ms=%d gemini_ms=%d upload_ms=%d objects=%d cells=%d tokens=%s",
        processing_ms, result.compose_ms, result.gemini_ms, result.upload_ms,
        result.object_count, result.cell_count, result.token_usage,
    )

    return SwapSpriteSheetResponse(
        success=True,
        data=SwapSpriteSheetCoreResultData(
            image_url=result.image_url,
            width=result.width,
            height=result.height,
            token_usage=result.token_usage,
            composed_sheet_url=result.composed_sheet_url,
            aiRequestId=result.ai_request_id,
        ),
        meta=SwapSpriteSheetMeta(
            processingTime=processing_ms,
            composeMs=result.compose_ms,
            geminiMs=result.gemini_ms,
            uploadMs=result.upload_ms,
            tokenUsage=result.token_usage,
            sheetDimensions=SpriteSheetDimensionsMeta(
                width=result.width, height=result.height,
            ),
            geminiPayloadBytes=SpriteGeminiPayloadBytesMeta(
                sheet=result.payload_bytes_sheet,
                humans=result.payload_bytes_humans,
            ),
            objectCount=result.object_count,
            cellCount=result.cell_count,
            swappedObjects=result.swapped_object_keys or None,
        ),
    )
