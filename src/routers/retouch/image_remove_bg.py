"""`image_remove_bg_core` + public HTTP route — Bria/851-labs remove-background.

CORE (`image_remove_bg_core` + `_decode_base64`) ported from image-api in P3b for
the in-process `remix_rmbg` stage job (always `return_bytes=True`). P3c ADDS the
public HTTP endpoint (`@router.post("/image-remove-bg")`) — the remix sub-app's
remove-bg tab calls it (URL-only public contract + `resolve_model_params` allowlist
pre-check + opt-in `save_generated_resource` no-op seam).

AUTH DELTA vs image-api: gated by the editor-session Bearer at the router group
level (NOT X-API-Key); the handler reads the session ctx for AI-usage audit.

Seam parity: `sb`/Storage/Replicate go through this service's adapters
(`get_storage` via `src.services.storage`, `run_remove_bg`). Every failure raises
via `error_response` (HTTPException) exactly as image-api.
"""

import asyncio
import base64
import binascii
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from PIL import UnidentifiedImageError

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.jobs.model_registry import resolve_model_params
from src.models.requests.image_remove_bg import (
    BRIA_REMOVE_BG_MODEL,
    REPLICATE_TIMEOUT_S,
    ImageRemoveBgCoreResult,
    ImageRemoveBgData,
    ImageRemoveBgMeta,
    ImageRemoveBgParams,
    ImageRemoveBgRequest,
    ImageRemoveBgResponse,
)
from src.routers._shared.deps import error_response, url_host
from src.services import ssrf_guard
from src.services.ai_usage import AiCallContext
from src.services.http_fetch import fetch_image_bytes
from src.services.image_ops import ensure_png, flatten_on_color, sniff_mime
from src.services.replicate_client import run_remove_bg
from src.services.resource_persist import (
    GeneratedResourceValue,
    PersistContext,
    save_generated_resource,
    save_response_fields,
)
from src.services.rmbg import get_remove_bg_adapter
from src.services.storage import build_remove_bg_path, upload_bytes

logger = logging.getLogger(__name__)

router = APIRouter()


_DATA_URI_PREFIX = "data:"
_ALLOWED_INPUT_MIMES = {"image/png", "image/jpeg", "image/webp"}
_MAX_DECODED_BYTES = 10 * 1024 * 1024
# Output-fetch cap for trusted in-process `imageBytes` callers (rmbg stage job 09).
_MAX_INPROCESS_OUTPUT_BYTES = 64 * 1024 * 1024


def _decode_base64(b64: str) -> tuple[str, bytes]:
    """Decode base64 → sniff mime → enforce size cap. Raises HTTPException on fail."""
    raw = b64.strip()
    if raw.startswith(_DATA_URI_PREFIX):
        _, _, payload = raw.partition(",")
        if not payload:
            raise error_response(
                422, "INVALID_IMAGE_DATA", "Malformed data URI (empty payload)"
            )
        b64_payload = payload
    else:
        b64_payload = raw
    try:
        data = base64.b64decode(b64_payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise error_response(422, "INVALID_IMAGE_DATA", "Malformed base64") from exc
    if not data:
        raise error_response(422, "INVALID_IMAGE_DATA", "Empty base64 payload")
    if len(data) > _MAX_DECODED_BYTES:
        raise error_response(
            413, "IMAGE_TOO_LARGE", "Decoded base64 exceeds size cap"
        )
    mime = sniff_mime(data[:256])
    if mime not in _ALLOWED_INPUT_MIMES:
        raise error_response(
            422, "INVALID_IMAGE_DATA", "Base64 is not png/jpeg/webp"
        )
    return mime, data


async def image_remove_bg_core(
    req: ImageRemoveBgRequest,
    *,
    ai_context: AiCallContext | None = None,
    operation: str | None = None,
) -> ImageRemoveBgCoreResult:
    """Run Bria/851-labs remove-background. Accepts URL or base64 or `imageBytes`.

    `return_bytes=True` → returns the processed PNG bytes in `image_bytes` and SKIPS
    Storage upload (the `remix_rmbg` path). `return_bytes=False` → uploads + returns
    `imageUrl`/`storagePath`.
    """
    start_ms = time.monotonic()

    # 1. Resolve source → `image_value` (URL or data URI) for Replicate.
    if req.imageUrl:
        image_value = req.imageUrl
        ssrf_guard.validate_public_url(image_value)
        log_src = url_host(image_value)
        source_type = "url"
    elif req.imageBytes is not None:
        raw_bytes = req.imageBytes
        if not raw_bytes:
            raise error_response(422, "INVALID_IMAGE_DATA", "imageBytes is empty")
        mime = sniff_mime(raw_bytes[:256])
        if mime not in _ALLOWED_INPUT_MIMES:
            raise error_response(
                422, "INVALID_IMAGE_DATA", "imageBytes is not png/jpeg/webp"
            )
        clean_b64 = await asyncio.to_thread(
            lambda: base64.b64encode(raw_bytes).decode("ascii")
        )
        image_value = f"data:{mime};base64,{clean_b64}"
        log_src = "(bytes)"
        source_type = "bytes"
    else:
        # imageBase64 is guaranteed by the request validator.
        mime, raw_bytes = await asyncio.to_thread(_decode_base64, req.imageBase64 or "")
        clean_b64 = base64.b64encode(raw_bytes).decode("ascii")
        image_value = f"data:{mime};base64,{clean_b64}"
        log_src = "(base64)"
        source_type = "base64"

    logger.debug(
        "remove_bg_start src=%s source_type=%s preserve_alpha=%s bg_color=%s return_bytes=%s",
        log_src, source_type, req.preserveAlpha, req.backgroundColor, req.return_bytes,
    )

    # 2. Replicate call. Per-model INPUT shape is owned by the adapter.
    adapter = get_remove_bg_adapter(req.model or BRIA_REMOVE_BG_MODEL)
    payload = adapter.build_payload(image_value, req.preserveAlpha)
    try:
        res = await run_remove_bg(
            payload,
            model=req.model,
            version=adapter.version,
            timeout_s=REPLICATE_TIMEOUT_S,
            ai_context=ai_context,
            operation=operation,
        )
        output_url = res.output
        prediction_id = res.prediction_id
        ai_request_id = res.ai_request_id
        # This service has no content-addressed re-host, so `output_files` is always
        # () → media_url stays None (parity: no durable raw-output URL).
        media_url = None
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("remove_bg_unexpected src=%s err=%s", log_src, exc)
        raise error_response(500, "INTERNAL_ERROR", "Unexpected error") from exc

    # 3. Fetch Replicate output bytes (raised cap for trusted in-process callers).
    output_cap = (
        _MAX_INPROCESS_OUTPUT_BYTES
        if req.imageBytes is not None
        else _MAX_DECODED_BYTES
    )
    try:
        image_bytes, src_ct = await fetch_image_bytes(
            output_url, max_bytes=output_cap, timeout_s=30.0
        )
    except HTTPException as exc:
        if exc.status_code == 504:
            raise
        logger.warning(
            "remove_bg_output_fetch_fail src=%s prediction_id=%s status=%s",
            log_src, prediction_id[:10] if prediction_id else "", exc.status_code,
        )
        raise error_response(
            502, "OUTPUT_FETCH_ERROR", "Failed to fetch Replicate output"
        ) from exc
    except Exception as exc:
        logger.error(
            "remove_bg_output_fetch_unexpected src=%s prediction_id=%s err=%s",
            log_src, prediction_id[:10] if prediction_id else "", exc,
        )
        raise error_response(
            502, "OUTPUT_FETCH_ERROR", "Failed to fetch Replicate output"
        ) from exc

    # 4. Postprocess to PNG. backgroundColor None → preserve transparent.
    if req.backgroundColor is None:
        try:
            png_bytes = await asyncio.to_thread(ensure_png, image_bytes, src_ct)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            logger.error(
                "remove_bg_decode_error src=%s prediction_id=%s src_ct=%s bytes=%d err=%s",
                log_src, prediction_id[:10] if prediction_id else "",
                src_ct, len(image_bytes), exc,
            )
            raise error_response(
                500, "IMAGE_PROCESSING_ERROR", "Failed to normalize output to PNG"
            ) from exc
    else:
        try:
            png_bytes = await asyncio.to_thread(
                flatten_on_color, image_bytes, req.backgroundColor
            )
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            logger.error(
                "remove_bg_flatten_error src=%s prediction_id=%s bg=%s bytes=%d err=%s",
                log_src, prediction_id[:10] if prediction_id else "",
                req.backgroundColor, len(image_bytes), exc,
            )
            raise error_response(
                500, "IMAGE_PROCESSING_ERROR",
                "Failed to flatten output on background color",
            ) from exc

    processing_ms = int((time.monotonic() - start_ms) * 1000)

    # 5. Bytes mode → return without upload.
    if req.return_bytes:
        logger.debug(
            "remove_bg_done_bytes src=%s processing_ms=%d prediction_id=%s",
            log_src, processing_ms,
            prediction_id[:10] if prediction_id else "",
        )
        return ImageRemoveBgCoreResult(
            imageUrl=None,
            storagePath=None,
            mimeType="image/png",
            replicatePredictionId=prediction_id or None,
            backgroundColor=req.backgroundColor,
            aiRequestId=ai_request_id,
            media_url=media_url,
            image_bytes=png_bytes,
        )

    # 6. URL mode → upload + return Storage URL.
    storage_path_seed = req.imageUrl or "image"
    storage_path = build_remove_bg_path(storage_path_seed)
    try:
        public_url = await upload_bytes(
            storage_path, png_bytes, content_type="image/png"
        )
    except Exception as exc:
        logger.error(
            "remove_bg_upload_error src=%s path=%s err=%s",
            log_src, storage_path, exc,
        )
        raise error_response(
            500, "STORAGE_UPLOAD_ERROR", "Storage upload failed"
        ) from exc

    logger.debug(
        "remove_bg_done src=%s processing_ms=%d path=%s prediction_id=%s",
        log_src, processing_ms, storage_path,
        prediction_id[:10] if prediction_id else "",
    )
    return ImageRemoveBgCoreResult(
        imageUrl=public_url,
        storagePath=storage_path,
        mimeType="image/png",
        replicatePredictionId=prediction_id or None,
        backgroundColor=req.backgroundColor,
        aiRequestId=ai_request_id,
        media_url=media_url,
        image_bytes=None,
    )


@router.post("/image-remove-bg", response_model=ImageRemoveBgResponse)
async def image_remove_bg(
    params: ImageRemoveBgParams,
    session: EditorSessionContext = Depends(require_editor_session),
) -> ImageRemoveBgResponse:
    """HTTP endpoint — URL-only public contract + optional `model` selection.

    Ported from image-api; only the auth seam changes (editor session, not
    X-API-Key). `model` is validated against the `rmbg` allowlist BEFORE binding
    (public-bound guard) — a bad id raises `RemixDomainError(422 UNSUPPORTED_MODEL)`
    surfaced by the dedicated handler in `main.py`.
    """
    start_ms = time.monotonic()
    image_url_str = str(params.imageUrl)
    preserve_alpha = params.preserveAlpha if params.preserveAlpha is not None else True

    if params.model is not None:
        resolve_model_params({"model": params.model}, "rmbg")

    core_req = ImageRemoveBgRequest(
        imageUrl=image_url_str,
        imageBase64=None,
        preserveAlpha=preserve_alpha,
        backgroundColor=params.backgroundColor,
        return_bytes=False,
        model=params.model,
    )
    # Attribution (dual-context, only-winner): remixId wins as the cost discriminator
    # (snapshot_id left NULL to avoid double-count); else snapshotId → book cost.
    # admin_ref/sid from the editor session ride into `request.audit`.
    if params.remixId:
        ai_context = AiCallContext(
            remix_id=params.remixId, admin_ref=session.admin_ref, sid=session.sid
        )
    elif params.snapshotId:
        ai_context = AiCallContext(
            snapshot_id=params.snapshotId, admin_ref=session.admin_ref, sid=session.sid
        )
    else:
        ai_context = AiCallContext(admin_ref=session.admin_ref, sid=session.sid)
    result = await image_remove_bg_core(core_req, ai_context=ai_context)

    # Opt-in auto-persist (no-op parity seam). Soft-fail never breaks the response.
    save_outcome = await save_generated_resource(
        params.saveResource,
        GeneratedResourceValue(
            media_url=result.media_url or result.imageUrl or "",
            storage_path=result.storagePath,
            ai_request_id=result.aiRequestId,
            original_url=image_url_str,
        ),
        PersistContext(snapshot_id=params.snapshotId, remix_id=params.remixId),
    )

    processing_ms = int((time.monotonic() - start_ms) * 1000)
    return ImageRemoveBgResponse(
        success=True,
        data=ImageRemoveBgData(
            imageUrl=result.imageUrl or "",
            storagePath=result.storagePath or "",
            aiRequestId=result.aiRequestId,
            media_url=result.media_url,
            **save_response_fields(save_outcome),
        ),
        meta=ImageRemoveBgMeta(
            processingTime=processing_ms,
            mimeType=result.mimeType,
            replicatePredictionId=result.replicatePredictionId,
            backgroundColor=result.backgroundColor,
        ),
    )
