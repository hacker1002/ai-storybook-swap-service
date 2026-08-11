"""Service core for POST /api/image/upscale-image.

`run_upscale(UpscaleCoreRequest) -> UpscaleCoreResult` — framework-agnostic AI
upscaler dispatched via a per-model adapter (`services.upscale`: xinntao
default (Anime, ⚡2026-06-29), real-esrgan, alexgenovese, recraft-crisp). Generic,
stateless: no DB read/write;
self-contained Storage upload. Every failure path raises `ImageDomainError`
(NEVER HTTPException) so an in-process job (ADR-031) or the thin HTTP router can
both reuse it.

Model is resolved by `get_upscale_adapter(req.model or UPSCALE_DEFAULT_MODEL)`;
the adapter owns the Replicate payload, dispatch mode (version|model), and the
capability flags (`supports_scale`, `supports_face_enhance`, `max_input_pixels`).

Source contract — exactly one of:
  - `imageUrl`     : passed through to Replicate verbatim (SSRF-guarded).
  - `imageBase64`  : data-URI or raw base64; decoded (≤10MB), sniffed (png/
                     jpeg/webp), re-wrapped as a clean `data:{mime};base64,…`
                     URI for Replicate. NOT SSRF-guarded (no network fetch).

Single vs tiled paths (auto-decided after measuring src dims; gated on the
adapter's `max_input_pixels` cap):
  - cap is None (fixed-ratio, recraft) → SINGLE always: native passthrough, no
    post-resize, `fixedRatio=True`. No tile, no INPUT_TOO_LARGE px-reject.
  - cap set, src_px ≤ cap → SINGLE: 1 Replicate call, fetch output, upload.
  - cap set, src_px > cap, N := ceil(src_px / cap) ≤ TILE_MAX_COUNT → TILED:
    split into N strips along long axis, upscale each in parallel
    (asyncio.gather), blend with linear feather over TILE_OVERLAP_INPUT_PX ×
    scale output pixels, upload composite. Atomic failure (1 tile fail →
    request fail; no retry).
  - cap set, N > TILE_MAX_COUNT → 422 INPUT_TOO_LARGE_FOR_MODEL.

Output is sniffed and re-encoded to PNG if the model emits another format
(`_ensure_png_blocking`) — the endpoint contract is always image/png.

Output (single Replicate URL or N tile URLs) is fetched (≤20MB each), measured/
blended, then uploaded once. `fetch_image_bytes` raises HTTPException; we remap
to ImageDomainError so the "core only raises ImageDomainError" contract holds.

Tracing: `@traceable(name="image.upscale")` — counts/dims only; never logs
bytes/base64/full URLs (host-only).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from io import BytesIO
from urllib.parse import urlparse

import replicate
from fastapi import HTTPException
from langsmith import traceable
from PIL import Image, UnidentifiedImageError

from src.jobs.model_registry import UPSCALE_DEFAULT_MODEL
from src.models.requests.upscale_image import (
    ALLOWED_INPUT_MIMES,
    GRAIN_MAX_PIXELS,
    GrainParams,
    INPUT_FETCH_MAX_BYTES,
    INPUT_FETCH_TIMEOUT_S,
    MAX_DECODED_BYTES,
    OUTPUT_FETCH_MAX_BYTES,
    OUTPUT_FETCH_TIMEOUT_S,
    REAL_ESRGAN_SAFE_LONGEST_EDGE_PX,
    REPLICATE_TIMEOUT_S,
    TILE_CONCURRENCY,
    TILE_MAX_COUNT,
    TILE_OVERLAP_INPUT_PX,
    TILE_RETRY_MAX_ATTEMPTS,
    UpscaleCoreRequest,
    UpscaleCoreResult,
)
from src.services.http_fetch import fetch_image_bytes
from src.services.upscale import UpscaleAdapter, get_upscale_adapter
from src.services.image.errors import ImageDomainError
from src.services.image.grain import apply_watercolor_grain
from src.services.image.tile_upscale import (
    TileBox,
    blend_tiles,
    compute_tile_layout,
    encode_png,
    extract_tile_bytes,
)
from src.services.image_ops import measure_size, sniff_mime
from src.services.ai_usage import AiCallContext, new_request_id
from src.services.replicate_client import (
    _extract_predict_time,
    _extract_url,
    _host,
    _is_fetch_error,
    _log_replicate_call,
    create_with_429_retry,
    get_replicate_client,
    replicate_prediction_slot,
)
from src.services.ssrf_guard import validate_public_url
from src.services.storage import (
    StorageUploadError,
    build_upscale_path,
    upload_bytes,
)

logger = logging.getLogger(__name__)

_DATA_URI_PREFIX = "data:"


def _origin_from_url(url: str) -> str:
    """Derive a filename slug (no extension) from a URL path for the storage
    path. Mirrors the sibling `build_*_path` helpers."""
    base = urlparse(url).path.rsplit("/", 1)[-1] or "image"
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or "image"


def _decode_and_sniff_base64(b64: str) -> tuple[str, bytes]:
    """Blocking: decode base64 → cap → sniff mime. Caller wraps in to_thread.

    Raises:
      ValueError("INVALID_IMAGE_DATA") — malformed base64 / unrecognized or
        disallowed image type.
      ValueError("IMAGE_TOO_LARGE")    — decoded payload exceeds the cap.
    """
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("INVALID_IMAGE_DATA") from exc
    if not data:
        raise ValueError("INVALID_IMAGE_DATA")
    if len(data) > MAX_DECODED_BYTES:
        raise ValueError("IMAGE_TOO_LARGE")
    mime = sniff_mime(data[:256])
    if mime not in ALLOWED_INPUT_MIMES:
        raise ValueError("INVALID_IMAGE_DATA")
    return mime, data


async def _resolve_source(
    req: UpscaleCoreRequest,
) -> tuple[str, str, str, bytes | None]:
    """Return (image_value, source_type, origin_name, decoded_bytes_or_none).

    URL → passthrough value (SSRF-guarded); decoded_bytes=None (caller fetches
    separately for the dimension pre-check). base64 → clean re-wrapped data URI
    plus the already-decoded raw bytes (avoids a second decode in the pre-check).
    bytes (in-process callers only) → wraps raw bytes in a clean data URI for
    Replicate; SKIPS `_decode_and_sniff_base64` (bypasses 10 MB cap) because
    bytes come from a trusted internal source. Mime sniff still applied.
    """
    sources_set = sum(
        1 for v in (req.imageUrl, req.imageBase64, req.imageBytes) if v
    )
    if sources_set != 1:
        raise ImageDomainError(
            status=422,
            code="INVALID_IMAGE_SOURCE",
            message=(
                "Exactly one of imageUrl, imageBase64, or imageBytes is required"
            ),
            details={"sourcesProvided": sources_set},
        )

    if req.imageUrl:
        try:
            validate_public_url(req.imageUrl)
        except HTTPException as exc:
            code = _detail_code(exc) or "SSRF_BLOCKED"
            raise ImageDomainError(
                status=exc.status_code or 400,
                code=code,
                message="Input URL blocked by SSRF guard",
            ) from exc
        return req.imageUrl, "url", _origin_from_url(req.imageUrl), None

    if req.imageBytes is not None:
        data = req.imageBytes
        if not data:
            raise ImageDomainError(
                status=422,
                code="INVALID_IMAGE_DATA",
                message="imageBytes payload is empty",
            )
        mime = sniff_mime(data[:256])
        if mime not in ALLOWED_INPUT_MIMES:
            raise ImageDomainError(
                status=422,
                code="INVALID_IMAGE_DATA",
                message="imageBytes is not a supported image (png/jpeg/webp)",
            )
        clean_b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{clean_b64}", "bytes", "image", data

    raw = req.imageBase64.strip()
    if raw.startswith(_DATA_URI_PREFIX):
        _, _, b64 = raw.partition(",")
        if not b64:
            raise ImageDomainError(
                status=422,
                code="INVALID_IMAGE_DATA",
                message="Malformed data URI (empty payload)",
            )
    else:
        b64 = raw
    try:
        mime, data = await asyncio.to_thread(_decode_and_sniff_base64, b64)
    except ValueError as exc:
        reason = str(exc)
        status = 413 if reason == "IMAGE_TOO_LARGE" else 422
        raise ImageDomainError(
            status=status,
            code=reason,
            message=(
                "Decoded base64 exceeds size cap"
                if reason == "IMAGE_TOO_LARGE"
                else "Base64 is not a supported image (png/jpeg/webp)"
            ),
        ) from exc
    clean_b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{clean_b64}", "base64", "image", data


async def _ensure_input_bytes(
    image_value: str, source_type: str, src_bytes: bytes | None
) -> bytes:
    """Return the source image bytes — fetch from URL if not already decoded.

    base64 path: `src_bytes` is already populated by `_resolve_source`. URL
    path: do one SSRF-guarded fetch (the helper validates publicly) — we need
    bytes for size measurement AND for potential tile-split.
    """
    if src_bytes is not None:
        return src_bytes
    try:
        data, _ = await fetch_image_bytes(
            image_value,
            max_bytes=INPUT_FETCH_MAX_BYTES,
            timeout_s=INPUT_FETCH_TIMEOUT_S,
        )
        return data
    except HTTPException as exc:
        raise ImageDomainError(
            status=422,
            code="IMAGE_FETCH_ERROR",
            message="Failed to fetch input image",
        ) from exc


def _detail_code(exc: HTTPException) -> str | None:
    """Best-effort read of `detail.error.code` from a sibling HTTPException."""
    detail = exc.detail
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, str):
                return code
    return None


async def _predict_single(
    adapter: UpscaleAdapter,
    image_value: str,
    scale: float,
    face_enhance: bool,
    label: str,
    *,
    retry_max_attempts: int = 2,
    create_sem: asyncio.Semaphore | None = None,
    ai_context: AiCallContext | None = None,
    operation: str = "image.upscale",
) -> tuple[str, str, float | None, str]:
    """One Replicate predict + wait. Returns (output_url, prediction_id, predict_time, ai_request_id).

    `predict_time` is `metrics.predict_time` (None when the SDK omits it) — each
    tile is a separately-billed prediction, so the Đợt-2 cost hook records it
    per tile (see `run_upscale` per-tile list).

    `adapter` owns the per-model payload (`build_payload`) + dispatch mode
    (`version` set → community pinned `version=`; None → official `model=`).
    ⚡Validation S1: all v1 upscale adapters pin `version=`, so the `model=`
    branch is forward-compat only (kept for parity with `rmbg` dispatch).

    `create_sem`: optional shared semaphore that wraps ONLY the rate-limited
    `predictions.async_create` POST. `async_wait` (GPU poll) runs OUTSIDE the
    sem so parallel tiles can wait concurrently for their GPU jobs. Pass None
    for single-pass (no concurrency contention).

    Raises `ImageDomainError` on any failure path (timeout/429/replicate-error/
    non-succeeded/empty-output/fetch-keyword). Caller fetches the output URL
    separately. `retry_max_attempts` is forwarded to `create_with_429_retry`;
    tile callers bump it to absorb 429 bursts.
    """
    payload = adapter.build_payload(image_value, scale, face_enhance)

    client = get_replicate_client()

    def _dispatch():
        # version is not None (NOT truthiness) — `version=""` must hard-fail,
        # not silently fall through to model=.
        if adapter.version is not None:
            return client.predictions.async_create(
                version=adapter.version, input=payload
            )
        return client.predictions.async_create(
            model=adapter.model_id, input=payload
        )

    if create_sem is None:
        async def _create():
            return await _dispatch()
    else:
        async def _create():
            async with create_sem:
                return await _dispatch()

    ctx = ai_context or AiCallContext()
    rid = new_request_id()  # BEFORE create; one ai_service_logs row per prediction
    prediction = None
    try:
        try:
            # Global Replicate in-flight bound (2026-06-12) — serializes
            # predictions ACROSS job types (rmbg 09 ∥ upscale 10). Wraps the leaf
            # create+wait only; the per-request `create_sem` (tile mode) nests
            # inside and stays as the create-POST rate-limit guard.
            async with replicate_prediction_slot():
                prediction = await create_with_429_retry(
                    _create,
                    label=label,
                    max_attempts=retry_max_attempts,
                )
                await asyncio.wait_for(
                    prediction.async_wait(), timeout=REPLICATE_TIMEOUT_S
                )
        except asyncio.TimeoutError as exc:
            logger.warning("upscale_timeout label=%s timeout_s=%s", label, REPLICATE_TIMEOUT_S)
            raise ImageDomainError(
                status=504, code="TIMEOUT", message="Upscale timed out"
            ) from exc
        except replicate.exceptions.ReplicateError as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("upscale_rate_limited label=%s", label)
                raise ImageDomainError(
                    status=429,
                    code="REPLICATE_RATE_LIMIT",
                    message="Replicate rate limited",
                ) from exc
            logger.error("upscale_replicate_error label=%s err=%s", label, exc)
            raise ImageDomainError(
                status=502, code="REPLICATE_ERROR", message=str(exc)[:200]
            ) from exc
        except Exception as exc:  # noqa: BLE001 — any other upstream failure
            logger.error("upscale_unexpected_error label=%s err=%s", label, exc)
            raise ImageDomainError(
                status=502, code="REPLICATE_ERROR", message=str(exc)[:200]
            ) from exc

        if prediction.status != "succeeded":
            err_msg = prediction.error or "prediction failed"
            if _is_fetch_error(prediction.error):
                logger.warning(
                    "upscale_fetch_error label=%s status=%s err=%s",
                    label,
                    prediction.status,
                    err_msg,
                )
                raise ImageDomainError(
                    status=422, code="IMAGE_FETCH_ERROR", message=str(err_msg)[:200]
                )
            logger.error(
                "upscale_non_succeeded label=%s status=%s err=%s",
                label,
                prediction.status,
                err_msg,
            )
            raise ImageDomainError(
                status=502, code="REPLICATE_ERROR", message=str(err_msg)[:200]
            )

        output_url = _extract_url(prediction.output)
        if not output_url:
            logger.error(
                "upscale_empty_output label=%s type=%s",
                label,
                type(prediction.output).__name__,
            )
            raise ImageDomainError(
                status=502,
                code="REPLICATE_ERROR",
                message="Replicate returned empty output",
            )

        prediction_id = getattr(prediction, "id", "") or ""
        predict_time = _extract_predict_time(prediction)
        # swap-service divergence: no content-addressed re-host (`_persist_outputs`
        # absent) — the raw output URL is recorded as metadata by the logger via
        # `output_urls`. Each tile is a separately-billed prediction = 1 row.
        _log_replicate_call(
            ctx=ctx, operation=operation, model=adapter.model_id,
            prediction=prediction, inputs=payload, status="success", output=output_url,
            output_urls=[output_url],
        )
        return output_url, prediction_id, predict_time, rid
    except ImageDomainError as exc:
        # Prediction ran then failed → error row (predict_time observability, no
        # cost). 429 create-throttle keeps `prediction is None` → no row.
        if prediction is not None:
            _log_replicate_call(
                ctx=ctx, operation=operation, model=adapter.model_id,
                prediction=prediction, inputs=payload, status="error",
                error=f"{exc.code}: {exc.message}",
            )
        raise


async def _fetch_replicate_output(output_url: str, prediction_id: str) -> bytes:
    """Fetch a Replicate output URL to bytes; map HTTPException → ImageDomainError."""
    try:
        data, _ = await fetch_image_bytes(
            output_url,
            max_bytes=OUTPUT_FETCH_MAX_BYTES,
            timeout_s=OUTPUT_FETCH_TIMEOUT_S,
        )
        return data
    except HTTPException as exc:
        if exc.status_code == 504:
            logger.warning(
                "upscale_output_fetch_timeout prediction_id=%s", prediction_id[:10]
            )
            raise ImageDomainError(
                status=504, code="TIMEOUT", message="Output fetch timed out"
            ) from exc
        logger.warning(
            "upscale_output_fetch_fail prediction_id=%s status=%s",
            prediction_id[:10],
            exc.status_code,
        )
        raise ImageDomainError(
            status=502,
            code="OUTPUT_FETCH_ERROR",
            message="Failed to fetch Replicate output",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "upscale_output_fetch_unexpected prediction_id=%s err=%s",
            prediction_id[:10],
            exc,
        )
        raise ImageDomainError(
            status=502,
            code="OUTPUT_FETCH_ERROR",
            message="Failed to fetch Replicate output",
        ) from exc


async def _predict_and_fetch_for_tile(
    adapter: UpscaleAdapter,
    tile_bytes: bytes,
    scale: float,
    face_enhance: bool,
    label: str,
    *,
    create_sem: asyncio.Semaphore,
    ai_context: AiCallContext | None = None,
    operation: str = "image.upscale",
) -> tuple[bytes, str, float | None]:
    """Tile path per-call: encode → data URI → predict (sem'd create + parallel
    wait) → fetch. Returns (output_bytes, prediction_id, predict_time).

    Each tile is a SEPARATELY-billed prediction → `_predict_single` logs one
    ai_service_logs row per tile (its `ai_request_id` is discarded here — the
    tiled result carries no single response id).

    Only the Replicate CREATE POST acquires `create_sem` — the slow GPU poll
    (`async_wait`) and output fetch run outside the lock so all N tile waits
    overlap. On any failure, `asyncio.gather` propagates and cancels siblings
    (atomic semantics). Only reachable for adapters with a non-None
    `max_input_pixels` (fixed-ratio adapters never tile).
    """
    b64 = base64.b64encode(tile_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64}"
    output_url, prediction_id, predict_time, _rid = await _predict_single(
        adapter,
        data_uri,
        scale,
        face_enhance,
        label,
        retry_max_attempts=TILE_RETRY_MAX_ATTEMPTS,
        create_sem=create_sem,
        ai_context=ai_context,
        operation=operation,
    )
    output_bytes = await _fetch_replicate_output(output_url, prediction_id)
    return output_bytes, prediction_id, predict_time


def _split_into_tiles_blocking(
    src_bytes: bytes,
    layout: list[TileBox],
) -> list[bytes]:
    """Blocking helper: decode source once, crop+encode each tile. Caller wraps."""
    img = Image.open(BytesIO(src_bytes))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")
    return [extract_tile_bytes(img, t) for t in layout]


def _blend_blocking(
    output_bytes_list: list[bytes],
    layout: list[TileBox],
    *,
    axis,
    scale: float,
    output_size: tuple[int, int],
) -> tuple[bytes, int, int]:
    """Blocking helper: decode all tile outputs, blend, encode PNG.

    Returns (png_bytes, width, height) of the composite.
    """
    tiles_img: list[Image.Image] = []
    for b in output_bytes_list:
        try:
            t = Image.open(BytesIO(b))
            t.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"tile output not decodable: {exc}") from exc
        tiles_img.append(t)
    composite = blend_tiles(
        tiles_img,
        layout,
        axis=axis,
        scale=scale,
        overlap_input=TILE_OVERLAP_INPUT_PX,
        output_size=output_size,
    )
    return encode_png(composite), composite.size[0], composite.size[1]


def _ensure_png_blocking(data: bytes) -> bytes:
    """Format guard (Issue E): re-encode to PNG if the model output is not PNG.

    Some upscalers (recraft fixed-ratio) may emit JPEG/WebP; the endpoint
    contract is always `image/png`. real-esrgan / alexgenovese already return
    PNG so this is a sniff-only no-op for them. Blocking (Pillow) — caller wraps
    in `asyncio.to_thread`. Preserves dims (re-encode does not resize), so it is
    safe to apply AFTER measuring output dims.
    """
    if sniff_mime(data[:256]) == "image/png":
        return data
    img = Image.open(BytesIO(data))
    img.load()
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@traceable(name="image.upscale")
async def run_upscale(
    req: UpscaleCoreRequest,
    *,
    ai_context: AiCallContext | None = None,
    operation: str = "image.upscale",
) -> UpscaleCoreResult:
    """Upscale via an adapter-selected Replicate model, auto-tiled when the model
    has a GPU pixel cap and src exceeds it. Raises only `ImageDomainError`.

    Model is resolved via `get_upscale_adapter` (post-allowlist; KeyError = config
    drift, bubbles as a programmer error). Fixed-ratio adapters (recraft,
    `max_input_pixels=None`) skip the tile gate entirely and pass through native
    dims (`fixedRatio=True`).

    `ai_context` (attribution) + `operation` (run_name; jobs/10 passes
    `remix.upscale`) thread to each prediction's ai_service_logs row. Single path
    surfaces the one prediction's `ai_request_id` on the result; tiled → None
    (the composite is not a single provider output).
    """
    image_value, source_type, origin, src_bytes = await _resolve_source(req)
    log_src = _host(image_value) if source_type == "url" else "(base64)"

    adapter = get_upscale_adapter(req.model or UPSCALE_DEFAULT_MODEL)
    cap = adapter.max_input_pixels
    # Weight-variant echo (xinntao only; None for adapters without a `variant`).
    adapter_variant = getattr(adapter, "variant", None)

    src_bytes = await _ensure_input_bytes(image_value, source_type, src_bytes)
    try:
        src_w, src_h = await asyncio.to_thread(measure_size, src_bytes)
    except ValueError as exc:
        raise ImageDomainError(
            status=422,
            code="INVALID_IMAGE_DATA",
            message="Input is not a decodable image",
        ) from exc

    src_px = src_w * src_h
    # Issue A — the tile / INPUT_TOO_LARGE gate applies ONLY to adapters with a
    # GPU pixel cap. Fixed-ratio adapters (recraft, cap=None) NEVER tile and
    # NEVER px-reject: always a single hosted call, native passthrough.
    if cap is not None:
        n_required = max(1, -(-src_px // cap))
        if n_required > TILE_MAX_COUNT:
            effective_cap = TILE_MAX_COUNT * cap
            logger.warning(
                "upscale_input_too_large src=%s model=%s w=%d h=%d px=%d effective_cap=%d max_tiles=%d",
                log_src, adapter.model_id, src_w, src_h, src_px, effective_cap, TILE_MAX_COUNT,
            )
            raise ImageDomainError(
                status=422,
                code="INPUT_TOO_LARGE_FOR_MODEL",
                message=(
                    f"Input image is {src_w}x{src_h} ({src_px:,}px). "
                    f"Requires more than {TILE_MAX_COUNT} tiles to fit the model GPU cap "
                    f"(per-tile {cap:,}px); effective input cap is "
                    f"{effective_cap:,}px. Downscale the input first."
                ),
                details={
                    "inputWidth": src_w,
                    "inputHeight": src_h,
                    "inputPixels": src_px,
                    "effectiveCapPixels": effective_cap,
                    "maxTiles": TILE_MAX_COUNT,
                    "suggestedLongestEdge": REAL_ESRGAN_SAFE_LONGEST_EDGE_PX,
                },
            )
        single = n_required == 1
    else:
        n_required = 1
        single = True

    fixed_ratio = not adapter.supports_scale
    logger.info(
        "upscale_invoke src=%s scale=%s face=%s model=%s fixed_ratio=%s w=%d h=%d n_required=%d",
        log_src, req.scale, req.faceEnhance, adapter.model_id, fixed_ratio,
        src_w, src_h, n_required,
    )

    if single:
        output_bytes, predictions, out_w, out_h, ai_request_id = await _single_path(
            adapter, image_value, src_bytes, req.scale, req.faceEnhance, log_src,
            ai_context=ai_context, operation=operation,
        )
    else:
        output_bytes, predictions, out_w, out_h, ai_request_id = await _tiled_path(
            adapter, src_bytes, src_w, src_h, req.scale, req.faceEnhance, log_src,
            ai_context=ai_context, operation=operation,
        )
    # `predictions` is the per-tile [(prediction_id, predict_time)] list (1 elem
    # in the single path). `replicatePredictionIds` stays the flat id list for
    # the existing public `meta.replicatePredictionId`; `predictions` carries the
    # per-tile predict_time for the Đợt-2 cost hook.
    prediction_ids = [p[0] for p in predictions]
    tile_count = len(predictions)

    # Format guard (Issue E): ensure PNG output regardless of model. No-op when
    # already PNG (real-esrgan / alexgenovese); recraft may emit JPEG/WebP.
    output_bytes = await asyncio.to_thread(_ensure_png_blocking, output_bytes)

    # ── 6c WATERCOLOR GRAIN (2026-06-29) ──────────────────────────────────
    # Model-agnostic monochrome grain — the LAST transform before upload, so it
    # covers every path (single / tiled composite / recraft passthrough) AND the
    # bytes-only caller. Tiled mode runs grain ONCE on the full blended composite
    # → no seam noise. Non-fatal: a grain failure or an over-cap image keeps the
    # pre-grain bytes (grainApplied=False) — grain never fails the upscale.
    # Job 10 passes grain=None here on purpose (it grains per-crop later).
    grain_applied = False
    grain_used: GrainParams | None = None
    if req.grain is not None and req.grain.enabled:
        out_px = out_w * out_h
        if out_px <= GRAIN_MAX_PIXELS:
            try:
                output_bytes = await asyncio.to_thread(
                    apply_watercolor_grain,
                    output_bytes,
                    amp=req.grain.amp,
                    blur=req.grain.blur,
                    seed=req.grain.seed,
                )
                grain_applied = True
                grain_used = req.grain
            except Exception as exc:  # noqa: BLE001 — grain is best-effort
                logger.warning(
                    "upscale_grain_failed src=%s w=%d h=%d err=%s",
                    log_src, out_w, out_h, exc,
                )
        else:
            logger.warning(
                "upscale_grain_skip_over_cap src=%s out_px=%d cap=%d",
                log_src, out_px, GRAIN_MAX_PIXELS,
            )

    # Bytes mode (rev5) — in-process callers (post-swap pipeline) skip Storage
    # upload to keep the pipeline a pure in-memory blob handoff.
    if req.return_bytes:
        logger.info(
            "upscale_done_bytes src=%s source_type=%s scale=%s w=%d h=%d tile_count=%d prediction_ids=%s",
            log_src, source_type, req.scale, out_w, out_h, tile_count,
            [p[:10] for p in prediction_ids],
        )
        return UpscaleCoreResult(
            imageUrl=None,
            storagePath=None,
            width=out_w,
            height=out_h,
            mimeType="image/png",
            scale=req.scale,
            sourceType=source_type,
            tileCount=tile_count,
            replicatePredictionIds=prediction_ids,
            predictions=predictions,
            fixedRatio=fixed_ratio,
            variant=adapter_variant,
            grainApplied=grain_applied,
            grain=grain_used,
            image_bytes=output_bytes,
            ai_request_id=ai_request_id,
        )

    storage_path = build_upscale_path(origin, req.scale)
    try:
        public_url = await upload_bytes(
            storage_path, output_bytes, content_type="image/png"
        )
    except StorageUploadError as exc:
        logger.error("upscale_upload_error path=%s err=%s", storage_path, exc)
        raise ImageDomainError(
            status=500,
            code="STORAGE_UPLOAD_ERROR",
            message="Storage upload failed",
            details={"path": storage_path},
        ) from exc

    logger.info(
        "upscale_done src=%s source_type=%s scale=%s w=%d h=%d tile_count=%d prediction_ids=%s",
        log_src, source_type, req.scale, out_w, out_h, tile_count,
        [p[:10] for p in prediction_ids],
    )

    return UpscaleCoreResult(
        imageUrl=public_url,
        storagePath=storage_path,
        width=out_w,
        height=out_h,
        mimeType="image/png",
        scale=req.scale,
        sourceType=source_type,
        tileCount=tile_count,
        replicatePredictionIds=prediction_ids,
        predictions=predictions,
        fixedRatio=fixed_ratio,
        variant=adapter_variant,
        grainApplied=grain_applied,
        grain=grain_used,
        ai_request_id=ai_request_id,
    )


async def _single_path(
    adapter: UpscaleAdapter,
    image_value: str,
    src_bytes: bytes,
    scale: float,
    face_enhance: bool,
    log_src: str,
    *,
    ai_context: AiCallContext | None = None,
    operation: str = "image.upscale",
) -> tuple[bytes, list[tuple[str, float | None]], int, int, str]:
    """SINGLE PATH: 1 Replicate call → fetch output → measure → return.

    `image_value` is passed verbatim (URL or already-clean data URI from
    `_resolve_source`). `src_bytes` unused here but threaded for symmetry. Output
    dims are MEASURED from the model output (not src×scale) — for fixed-ratio
    adapters (recraft) this naturally yields native passthrough dims.

    The prediction list is a 1-element `[(prediction_id, predict_time)]` — the
    uniform per-tile shape shared with `_tiled_path`. The trailing element is the
    single prediction's `ai_request_id` (`run_upscale` maps it to
    `UpscaleCoreResult.ai_request_id`; tiled path returns None instead).
    """
    del src_bytes  # noqa — kept for callsite symmetry
    output_url, prediction_id, predict_time, rid = await _predict_single(
        adapter, image_value, scale, face_enhance, "upscale",
        ai_context=ai_context, operation=operation,
    )
    output_bytes = await _fetch_replicate_output(output_url, prediction_id)
    try:
        out_w, out_h = await asyncio.to_thread(measure_size, output_bytes)
    except ValueError as exc:
        logger.error(
            "upscale_measure_fail prediction_id=%s err=%s", prediction_id[:10], exc
        )
        raise ImageDomainError(
            status=502,
            code="OUTPUT_FETCH_ERROR",
            message="Replicate output is not a decodable image",
        ) from exc
    logger.info(
        "upscale_single_done src=%s w=%d h=%d prediction_id=%s",
        log_src, out_w, out_h, prediction_id[:10],
    )
    return output_bytes, [(prediction_id, predict_time)], out_w, out_h, rid


async def _tiled_path(
    adapter: UpscaleAdapter,
    src_bytes: bytes,
    src_w: int,
    src_h: int,
    scale: float,
    face_enhance: bool,
    log_src: str,
    *,
    ai_context: AiCallContext | None = None,
    operation: str = "image.upscale",
) -> tuple[bytes, list[tuple[str, float | None]], int, int, None]:
    """TILED PATH: split → parallel predict+fetch → blend → return.

    Each tile is a separate prediction (its own ai_service_logs row, logged
    inside `_predict_single`); the blended composite is NOT a single provider
    output, so the trailing `ai_request_id` is None (no `data.aiRequestId`).

    Only reachable for adapters with a non-None `max_input_pixels` (fixed-ratio
    adapters never tile). Atomic semantics: if any tile raises,
    `asyncio.gather(return_exceptions=False)` cancels pending tasks and re-raises
    the first exception. Successful tiles' prediction cost is forfeit — accepted
    trade-off for predictable output.
    """
    layout, axis = compute_tile_layout(
        src_w,
        src_h,
        max_pixels=adapter.max_input_pixels,
        overlap=TILE_OVERLAP_INPUT_PX,
    )
    n = len(layout)

    try:
        tile_input_bytes_list = await asyncio.to_thread(
            _split_into_tiles_blocking, src_bytes, layout
        )
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDomainError(
            status=422,
            code="INVALID_IMAGE_DATA",
            message="Input image cannot be decoded for tile split",
        ) from exc

    logger.info(
        "upscale_tile_split src=%s n=%d axis=%s tile_dims=%s",
        log_src,
        n,
        axis,
        [(t.crop_w, t.crop_h) for t in layout],
    )

    create_sem = asyncio.Semaphore(TILE_CONCURRENCY)
    coros = [
        _predict_and_fetch_for_tile(
            adapter,
            tile_input_bytes_list[i],
            scale,
            face_enhance,
            f"upscale_tile_{i+1}_of_{n}",
            create_sem=create_sem,
            ai_context=ai_context,
            operation=operation,
        )
        for i in range(n)
    ]
    tile_results: list[tuple[bytes, str, float | None]] = await asyncio.gather(
        *coros
    )

    tile_output_bytes = [r[0] for r in tile_results]
    predictions = [(r[1], r[2]) for r in tile_results]

    out_w = int(round(src_w * scale))
    out_h = int(round(src_h * scale))
    try:
        composite_bytes, comp_w, comp_h = await asyncio.to_thread(
            _blend_blocking,
            tile_output_bytes,
            layout,
            axis=axis,
            scale=scale,
            output_size=(out_w, out_h),
        )
    except ValueError as exc:
        logger.error(
            "upscale_tile_blend_fail src=%s n=%d err=%s", log_src, n, exc
        )
        raise ImageDomainError(
            status=502,
            code="OUTPUT_FETCH_ERROR",
            message="A tile output is not decodable",
        ) from exc

    logger.info(
        "upscale_tile_done src=%s n=%d w=%d h=%d",
        log_src, n, comp_w, comp_h,
    )
    return composite_bytes, predictions, comp_w, comp_h, None
