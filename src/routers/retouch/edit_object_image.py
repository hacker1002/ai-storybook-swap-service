"""POST /api/retouch/edit-object-image — Gemini image editing + App Storage upload (P3c port).

Ported from `ai-storybook-image-api/src/routers/retouch/edit_object_image.py`. Step
order is VERBATIM (validate → fetch refs → build parts → `gemini_ainvoke` → parse →
upload → response); only the SEAMS change:
  - AUTH: editor-session Bearer at the router group level (NOT X-API-Key). The
    handler declares `require_editor_session` to read the session ctx (admin_ref/sid)
    for AI-usage audit — FastAPI dedups the router+handler dependency.
  - `@traceable` decorator DROPPED: on a FastAPI handler it is a no-op (memory
    `traceable_route_handler_bypass`); the run_name rides through `gemini_ainvoke`.
  - Storage/prompt/model/log seams are this service's shared modules (already the
    same import paths as image-api via the compat shims).

ENVELOPE: keeps image-api's OWN contract — `error_response` (HTTPException →
`{detail:{success,error}}`) for step failures, `RemixDomainError` (dedicated
handler) for UNSUPPORTED_MODEL. NOT the `/api/editor/*` envelope.
"""

import asyncio
import base64
import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.models.requests.edit_object_image import (
    EDIT_OBJECT_IMAGE_SYSTEM_NAME,
    GEMINI_TEMPERATURE,
    GEMINI_TIMEOUT_S,
    MAX_IMAGE_BYTES,
    EditObjectImageData,
    EditObjectImageMeta,
    EditObjectImageParams,
    EditObjectImageResponse,
    ReferenceImage,
)
from src.routers._shared.deps import error_response, url_host
from src.services.ai_usage import AiCallContext
from src.services.gemini.invoke import gemini_ainvoke
from src.services.gemini.model_resolution import clamp_temperature, resolve_gemini_model
from src.services.gemini.response import (
    GeminiResponseError,
    classify_gemini_exc,
    extract_image,
)
from src.services.http_fetch import fetch_image_bytes
from src.services.image_ops import ensure_png, measure_size, nearest_aspect_ratio
from src.services.prompt_loader import (
    PromptTemplateNotFound,
    fetch_template_row,
    render_variables,
)
from src.services.reference_prompt_builder import (
    ReferenceRole,
    ReferenceSpec,
    build_references,
)
from src.services.resource_persist import (
    GeneratedResourceValue,
    PersistContext,
    save_generated_resource,
    save_response_fields,
)
from src.services.storage import build_edit_object_path, upload_bytes

logger = logging.getLogger(__name__)

router = APIRouter()

_EXT_MIME_MAP: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _normalize_mime(content_type: str | None, url: str) -> str:
    ct = (content_type or "").strip().lower()
    if ct.startswith("image/"):
        return ct
    path = urlparse(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    return _EXT_MIME_MAP.get(ext, "image/png")


def _decode_references(
    refs: list[ReferenceImage],
) -> list[tuple[bytes, str, str | None]]:
    """Decode each ref → (bytes, mime, description?). `description` (sanitized by
    the Pydantic validator) rides through into the map-guide line via the builder
    spec metadata; None → the map line keeps the bare `ẢNH THAM KHẢO` tag."""
    out: list[tuple[bytes, str, str | None]] = []
    for idx, r in enumerate(refs):
        try:
            data = base64.b64decode(r.base64Data, validate=True)
        except Exception as exc:
            raise error_response(
                400,
                "VALIDATION_ERROR",
                f"referenceImages[{idx}] invalid base64",
            ) from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise error_response(
                400,
                "VALIDATION_ERROR",
                f"referenceImages[{idx}] exceeds {MAX_IMAGE_BYTES} bytes",
            )
        out.append((data, r.mimeType, r.description))
    return out


def _decode_region(region: ReferenceImage) -> tuple[bytes, str]:
    """Decode the `regionAnnotation` base64 → (bytes, mime). No SSRF — region is
    client-supplied base64, not a URL."""
    try:
        data = base64.b64decode(region.base64Data, validate=True)
    except Exception as exc:
        raise error_response(
            400, "VALIDATION_ERROR", "regionAnnotation invalid base64"
        ) from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise error_response(
            400,
            "VALIDATION_ERROR",
            f"regionAnnotation exceeds {MAX_IMAGE_BYTES} bytes",
        )
    return data, region.mimeType


@router.post("/edit-object-image", response_model=EditObjectImageResponse)
async def edit_object_image(
    params: EditObjectImageParams,
    session: EditorSessionContext = Depends(require_editor_session),
) -> EditObjectImageResponse:
    start = time.monotonic()
    image_url_str = str(params.imageUrl)
    host = url_host(image_url_str)
    ref_count = len(params.referenceImages or [])

    logger.debug(
        "edit_object_image_start host=%s prompt_len=%d ref_count=%d aspect=%s size=%s "
        "region=%s model_override=%s",
        host, len(params.prompt), ref_count, params.aspectRatio, params.imageSize,
        params.regionAnnotation is not None, params.modelParams is not None,
    )

    # 1.5 Fetch the seed system prompt (fetch-only — need `default_model` early for
    # the fail-fast model resolve, and must render AFTER building the map guide).
    try:
        system_template, default_model = await fetch_template_row(
            EDIT_OBJECT_IMAGE_SYSTEM_NAME
        )
    except PromptTemplateNotFound as exc:
        logger.error(
            "edit_object_image_template_missing name=%s", EDIT_OBJECT_IMAGE_SYSTEM_NAME
        )
        raise error_response(
            500, "PROMPT_TEMPLATE_NOT_FOUND", "edit-object system prompt not seeded"
        ) from exc
    if not default_model:
        logger.error(
            "edit_object_image_template_no_model name=%s", EDIT_OBJECT_IMAGE_SYSTEM_NAME
        )
        raise error_response(
            500, "PROMPT_TEMPLATE_NOT_FOUND", "no image model configured for edit-object"
        )

    # 1.6 Resolve model + temperature FAIL-FAST — an UNSUPPORTED_MODEL (RemixDomainError
    # 422, surfaced by the global handler) raises HERE, before ANY source fetch / build /
    # Gemini dispatch. Omitted modelParams → DB model + GEMINI_TEMPERATURE (0.3).
    dispatch_model = resolve_gemini_model(
        param_public=params.modelParams.model if params.modelParams else None,
        db_model=default_model,
        group="edit-object",
    )
    temperature = clamp_temperature(
        params.modelParams.params.temperature
        if params.modelParams and params.modelParams.params
        else None,
        default=GEMINI_TEMPERATURE,
    )

    # Fetch source image (SSRF-guarded)
    fetch_start = time.monotonic()
    source_bytes, source_ct = await fetch_image_bytes(
        image_url_str, max_bytes=MAX_IMAGE_BYTES, timeout_s=30.0
    )
    source_mime = _normalize_mime(source_ct, image_url_str)
    logger.debug(
        "edit_object_image_fetched host=%s bytes=%d mime=%s fetch_ms=%d",
        host, len(source_bytes), source_mime,
        int((time.monotonic() - fetch_start) * 1000),
    )

    # Set-of-mark region path (only when `regionAnnotation` present). Decode the
    # marked-up SOURCE (base64, no SSRF) + guard the SOURCE ratio lands on the
    # requested aspectRatio enum — a mismatch → 422.
    region_bytes: bytes | None = None
    region_mime: str | None = None
    if params.regionAnnotation is not None:
        region_bytes, region_mime = _decode_region(params.regionAnnotation)
        try:
            src_w, src_h = await asyncio.to_thread(measure_size, source_bytes)
        except Exception as exc:
            raise error_response(
                422, "IMAGE_FETCH_ERROR", "Source image could not be decoded"
            ) from exc
        expected_ratio = nearest_aspect_ratio(src_w, src_h)
        if expected_ratio != params.aspectRatio:
            raise error_response(
                422,
                "REGION_ASPECT_MISMATCH",
                "regionAnnotation requires aspectRatio to match the source image ratio",
                details={
                    "sourceWidth": src_w,
                    "sourceHeight": src_h,
                    "expectedAspectRatio": expected_ratio,
                    "received": params.aspectRatio,
                },
            )

    # Decode reference images (keeps per-item `description` for the map guide line)
    ref_parts = _decode_references(params.referenceImages or [])

    # Conform to the shared reference-prompt-builder: ordering `[text, SOURCE,
    # REGION_MARK?, *ADDITIONAL]`. `guide_style="map"` → a THIN image→role map;
    # the per-role prose lives STATICALLY in the seed. Map ALWAYS has ≥1 line.
    specs = [ReferenceSpec(ReferenceRole.SOURCE, source_bytes, source_mime, {})]
    if region_bytes is not None:
        specs.append(
            ReferenceSpec(ReferenceRole.REGION_MARK, region_bytes, region_mime, {})
        )
    specs += [
        ReferenceSpec(
            ReferenceRole.ADDITIONAL,
            data,
            mime,
            {"description": desc} if desc else {},
        )
        for (data, mime, desc) in ref_parts
    ]
    built = build_references(specs, guide_style="map")

    # Render the seed system prompt — replaces `{%request.prompt%}` (user request)
    # + `{%request.reference_guide%}` (the map, always ≥1 line).
    text = render_variables(
        system_template,
        {"prompt": params.prompt, "reference_guide": built["guide_text"]},
    )

    content_parts: list[dict] = [{"type": "text", "text": text}, *built["parts"]]
    message = HumanMessage(content=content_parts)

    # AI-usage attribution — DUAL context (EditImageModal mounts book + remix). Stamp
    # ONLY the winner: `remixId` present ⇒ remix cost (discriminator, snapshot_id left
    # NULL to avoid double-counting), else `snapshotId` ⇒ book cost. admin_ref/sid from
    # the editor session ride into `request.audit` for cost attribution + audit.
    if params.remixId:
        ai_ctx = AiCallContext(
            remix_id=params.remixId, admin_ref=session.admin_ref, sid=session.sid
        )
    else:
        ai_ctx = AiCallContext(
            snapshot_id=params.snapshotId, admin_ref=session.admin_ref, sid=session.sid
        )

    # Gemini image edit via the shared `gemini_ainvoke` helper (ADR-049 — the ONE ctor
    # site). response_modalities=["IMAGE"] required for image output; image_config
    # forwards aspect_ratio + image_size. The helper propagates exceptions RAW.
    gemini_start = time.monotonic()
    logger.debug(
        "edit_object_image_gemini_invoke model=%s temperature=%s parts=%d",
        dispatch_model, temperature, len(content_parts),
    )
    try:
        result = await gemini_ainvoke(
            model=dispatch_model,
            messages=[message],
            run_name="retouch_edit_object_image",
            temperature=temperature,
            timeout_s=GEMINI_TIMEOUT_S,
            response_modalities=["IMAGE"],
            image_config={
                "aspect_ratio": params.aspectRatio,
                "image_size": params.imageSize,
            },
            ai_context=ai_ctx,
        )
    except HTTPException:
        raise
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - gemini_start) * 1000)
        status, code = classify_gemini_exc(exc)
        logger.error(
            "edit_object_image_gemini_error host=%s elapsed_ms=%d status=%d code=%s err_type=%s err=%s",
            host, elapsed_ms, status, code, type(exc).__name__, str(exc)[:300],
        )
        raise error_response(status, code, str(exc)[:200]) from exc

    response = result.message
    elapsed_ms = int((time.monotonic() - gemini_start) * 1000)
    token_usage = None
    usage_meta = getattr(response, "usage_metadata", None)
    if isinstance(usage_meta, dict):
        token_usage = usage_meta.get("total_tokens") or usage_meta.get("input_tokens")
    logger.debug(
        "edit_object_image_gemini_ok host=%s elapsed_ms=%d tokens=%s",
        host, elapsed_ms, token_usage,
    )

    # Extract inline image from AIMessage content
    try:
        image_bytes, mime = extract_image(response.content)
    except GeminiResponseError as exc:
        raise error_response(exc.status, exc.code, exc.message) from exc
    logger.debug(
        "edit_object_image_extracted out_bytes=%d mime=%s", len(image_bytes), mime,
    )

    # Contract guarantees PNG output — normalize if Gemini returns JPEG/WebP
    if mime != "image/png":
        try:
            image_bytes = ensure_png(image_bytes, mime)
        except Exception as exc:
            logger.error(
                "edit_object_image_png_convert_fail host=%s src_mime=%s err=%s",
                host, mime, exc,
            )
            raise error_response(
                502, "NO_IMAGE_RESPONSE", "Failed to normalize Gemini output to PNG"
            ) from exc
        mime = "image/png"

    # Upload to App Storage
    storage_path = build_edit_object_path(image_url_str)
    upload_start = time.monotonic()
    try:
        public_url = await upload_bytes(
            storage_path, image_bytes, content_type="image/png"
        )
    except Exception as exc:
        logger.error(
            "edit_object_image_storage_fail path=%s bytes=%d upload_ms=%d err_type=%s err=%s",
            storage_path, len(image_bytes),
            int((time.monotonic() - upload_start) * 1000),
            type(exc).__name__, exc,
        )
        raise error_response(
            500, "STORAGE_UPLOAD_ERROR", "Storage upload failed"
        ) from exc

    # Opt-in auto-persist (after storage upload, before return). No-op when the client
    # sent no `saveResource` (this service ships resource_persist as a no-op parity
    # seam); soft-fail never breaks the response.
    save_outcome = await save_generated_resource(
        params.saveResource,
        GeneratedResourceValue(
            media_url=public_url,
            storage_path=storage_path,
            ai_request_id=result.ai_request_id,
            original_url=image_url_str,
        ),
        PersistContext(snapshot_id=params.snapshotId, remix_id=params.remixId),
    )

    processing_ms = int((time.monotonic() - start) * 1000)
    logger.debug(
        "edit_object_image_done host=%s processing_ms=%d tokens=%s path=%s",
        host, processing_ms, token_usage, storage_path,
    )

    return EditObjectImageResponse(
        success=True,
        data=EditObjectImageData(
            imageUrl=public_url,
            storagePath=storage_path,
            aiRequestId=result.ai_request_id,
            **save_response_fields(save_outcome),
        ),
        meta=EditObjectImageMeta(
            processingTime=processing_ms,
            mimeType=mime,
            tokenUsage=token_usage,
            model=dispatch_model,
        ),
    )
