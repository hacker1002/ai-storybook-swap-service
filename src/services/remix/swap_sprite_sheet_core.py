"""Service core for POST /api/remix/swap-sprite-sheet.

Per-object per-trait swap on a sprite sheet. Generic stateless orchestration
(mirrors `swap_mix_sheet_core.run_swap_mix_sheet`, lighter — no per-cell locator,
no unchanged refs):
  compose sheet from crops[] → upload composed (optional) → fit sheet budget →
  SINGLE-SOURCE pass: fetch M human refs (FATAL on ANY failure) into
  `ordered_images = [sheet, human_1..M]`, set each obj.human_image_index = i+1 →
  shared human pool → build per-object per-trait prompt (image_guide +
  crop_manifest + swap_objects, ALL derived from the SAME ordered list) →
  hard-guard total base64 → Gemini call (SHARED `_gemini_sem`) → finish_reason
  safety check BEFORE extract → ensure PNG (NO resize — Gemini-native dim) →
  upload (or return raw bytes when `return_bytes`).

Single-source indexing (CRITICAL): the ordered-images list is the ONE source of
truth for both `image_guide` and `swap_objects[].human_image_index`. Because
HUMAN_FETCH_ERROR is FATAL (any human fail → abort), the list is never sparse →
indices are contiguous `2..M+1` and the two prompt vars can never drift.

NO resize: the output is returned at Gemini-native dim (~2K–4K, ≠ sheet_geometry
in general). The caller (job 02) measures the actual output dims to rescale crop
geometry when cutting.

Image order into Gemini (contract): `[prompt, sheet, human_1, human_2, ..., human_M]`.

Every failure path maps to `RemixDomainError` with the spec error code; the
router does not catch anything specific. PII discipline: NEVER log/echo URLs,
bytes, base64, `human_description`, or trait descriptions — error details carry
only `object_key` / `finish_reason`. `meta.swappedObjects` echoes object_key only.

Tracing: `@traceable(name="remix_swap_sprite_sheet")` — counts/dims only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Optional

from langchain_core.messages import HumanMessage
from langsmith import traceable

from src.models.gemini_image_models import (
    GEMINI_IMAGE_MODEL_ID,
    PUBLIC_TO_GEMINI_IMAGE,
)
from src.models.requests.build_crop_sheet import BuildCropSheetRequest, FrameStyle
from src.models.requests.swap_sprite_sheet import (
    SwapSpriteSheetCoreRequest,
    sprite_crops_as_base,
)
from src.services import image_ops  # noqa: F401 — Pillow setup side-effects
from src.services.gemini.payload_budget import (
    MAX_SPRITE_SHEET_BYTES,
    BudgetExceededError,
    enforce_total_base64_budget,
    fit_human_refs_pool,
    fit_to_budget,
)
from src.services.gemini.response import (
    GeminiResponseError,
    classify_gemini_exc,
    extract_image,
)
from src.services.ai_usage import AiCallContext
from src.services.gemini.invoke import gemini_ainvoke
from src.services.prompt_loader import PromptTemplateNotFound, load_and_render
from src.services.remix.crop_sheet_composer import compose_crop_sheet
from src.services.remix.errors import RemixDomainError
from src.services.remix.gemini_image_seams import (
    AUX_FETCH_MAX_BYTES,
    _fetch_one,
    _finish_reason,
    _gemini_sem,
    _SAFETY_FINISH_REASONS,
)
from src.services.remix.swap_image_helpers import (
    build_dated_path,
    ensure_png_native,
    snap_aspect_ratio,
)
from src.services.reference_prompt_builder import ReferenceRole, ReferenceSpec
from src.services.remix.swap_sprite_prompt_builder import (
    SpriteObjectInput,
    build_sprite_references,
)
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)


# --- Constants ------------------------------------------------------------

SYSTEM_PROMPT_NAME = "SWAP_SPRITE_SHEET_SYSTEM"
# Shared app-wide Gemini image identity (src/models/gemini_image_models.py).
GEMINI_MODEL_ID = GEMINI_IMAGE_MODEL_ID
GEMINI_TEMPERATURE = 0.25  # low — faithful in-place edit; reduces per-trait drift
# D1 (model_params wiring): the public→provider map lives in the core, NOT the
# registry (which stays provider-agnostic). `req.model` is the public allowlist id
# (e.g. "google/nano-banana-pro"); resolve it to the Gemini provider id here.
# Unknown/None → GEMINI_MODEL_ID default (parity).
_PUBLIC_TO_GEMINI: dict[str, str] = PUBLIC_TO_GEMINI_IMAGE
GEMINI_TIMEOUT_S = 150.0
GEMINI_IMAGE_SIZE = "4K"

STORAGE_SWAP_PREFIX = "sprite-sheet-swaps"
STORAGE_COMPOSED_PREFIX = "sprite-sheet-composed"

# Per-human SSRF/source fetch cap (decompression-bomb guard). Shared art cap —
# humans get pooled/shrunk afterwards by `fit_human_refs_pool`.
HUMAN_FETCH_MAX_BYTES = AUX_FETCH_MAX_BYTES  # 10MB


@dataclasses.dataclass(slots=True, frozen=True)
class SwapSpriteSheetCoreResult:
    width: int
    height: int
    token_usage: Optional[int]
    composed_sheet_url: Optional[str]
    compose_ms: int
    gemini_ms: int
    upload_ms: int
    payload_bytes_sheet: int
    payload_bytes_humans: int
    object_count: int
    cell_count: int
    swapped_object_keys: list[str]
    # Optional with defaults — image_url populated in URL-mode, image_bytes in bytes-mode.
    # `repr=False` on bytes to avoid leaking PNG payload in logs/error messages.
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = dataclasses.field(default=None, repr=False)
    # AI-usage contract (Phase 05): `ai_service_logs.id` of the Gemini swap call.
    ai_request_id: str = ""


# --- Internal helpers -----------------------------------------------------


def _build_compose_request(req: SwapSpriteSheetCoreRequest) -> BuildCropSheetRequest:
    """Project the sprite swap request into the sibling composer's contract."""
    return BuildCropSheetRequest(
        sheet_geometry=req.sheet_geometry,
        crops=sprite_crops_as_base(req.crops),
        frame=FrameStyle(),  # composer fills its mapping-constant defaults
        response_format="base64",  # unused by compose_crop_sheet (returns bytes)
    )


# --- Public API -----------------------------------------------------------


@traceable(name="remix_swap_sprite_sheet")
async def run_swap_sprite_sheet(
    req: SwapSpriteSheetCoreRequest,
    *,
    ai_context: AiCallContext | None = None,
) -> SwapSpriteSheetCoreResult:
    """End-to-end per-object per-trait sprite-sheet pipeline. Generic — no DB.

    `ai_context` (Phase 05) attributes the Gemini swap call: the sync endpoint
    passes `AiCallContext()` (remix_id absent from the body), the sprite-swap job
    (02) passes the job-row context (remix_id → separate remix billing bucket).
    """
    n_crops = len(req.crops)
    n_objects = len(req.swap_objects)
    sheet_w = req.sheet_geometry.width
    sheet_h = req.sheet_geometry.height

    logger.info(
        "remix_swap_sprite_start n_crops=%d n_objects=%d sheet=%dx%d return_composed=%s return_bytes=%s",
        n_crops, n_objects, sheet_w, sheet_h,
        req.return_composed_sheet, req.return_bytes,
    )

    # 1. Compose sheet (reuse sibling). ALL_CROPS_FAILED (422) bubbles up.
    compose_t0 = time.monotonic()
    composed = await compose_crop_sheet(_build_compose_request(req))
    compose_ms = int((time.monotonic() - compose_t0) * 1000)
    logger.debug(
        "remix_swap_sprite_composed composed=%d skipped=%d sheet_bytes=%d compose_ms=%d",
        composed.composed_count, len(composed.skipped),
        len(composed.png_bytes), compose_ms,
    )

    # 2. Optionally upload composed sheet (QA artifact).
    upload_ms = 0
    composed_sheet_url: Optional[str] = None
    if req.return_composed_sheet:
        composed_path = build_dated_path(STORAGE_COMPOSED_PREFIX)
        up_t0 = time.monotonic()
        try:
            composed_sheet_url = await upload_bytes(
                composed_path, composed.png_bytes, content_type="image/png"
            )
        except StorageUploadError as exc:
            raise RemixDomainError(
                status=500, code="STORAGE_UPLOAD_ERROR",
                message="Failed to upload composed sheet",
                details={"path": composed_path},
            ) from exc
        upload_ms += int((time.monotonic() - up_t0) * 1000)

    # 3. Fit composed sheet to the sprite sheet budget (6MB).
    try:
        sheet_for_gemini = await fit_to_budget(
            composed.png_bytes, MAX_SPRITE_SHEET_BYTES
        )
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR",
            message="Composed sheet exceeds Gemini sheet budget",
        ) from exc

    # 4. Fetch each human (FATAL on ANY failure — the human is the appearance
    #    source of an object). The BUILDER now owns indexing: it assigns each
    #    object's `human_image_index` from `swap_objects[]` order (single source),
    #    so the core only fetches bytes + carries opaque object metadata. Because
    #    HUMAN_FETCH_ERROR is fatal, the human list is never sparse → contiguous
    #    indices in the builder.
    raw_humans: list[bytes] = []
    object_inputs: list[SpriteObjectInput] = []
    for obj in req.swap_objects:
        try:
            raw = await _fetch_one(
                obj.human_image_url, "human", HUMAN_FETCH_MAX_BYTES
            )
        except RemixDomainError as exc:
            logger.warning(
                "remix_swap_sprite_human_fatal object_key=%s inner=%s",
                obj.object_key, exc.code,
            )
            raise RemixDomainError(
                status=422, code="HUMAN_FETCH_ERROR",
                message="human image fetch/decode failed (object appearance missing)",
                details={"object_key": obj.object_key, "inner_code": exc.code},
            ) from exc
        raw_humans.append(raw)
        object_inputs.append(
            SpriteObjectInput(
                object_key=obj.object_key,
                name=obj.object_context.name,
                swap_traits=[(t.type, t.description) for t in obj.swap_traits],
            )
        )

    # 5. Shared human pool → per-human budget = pool // M.
    try:
        humans_for_gemini = await fit_human_refs_pool(raw_humans, m=n_objects)
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR",
            message="Human reference images exceed shared pool budget after fit",
        ) from exc

    # 6. Build the atomic references (builder OWNS parts + index + manifest).
    #    Called once on the pooled (pre-guard) bytes to derive the guide/manifest
    #    vars the prompt needs. guide_text/cell_swap_plan are byte-independent →
    #    the post-guard rebuild (step 8) yields identical text.
    def _build_specs(
        sheet: bytes, humans: list[bytes]
    ) -> tuple[ReferenceSpec, list[ReferenceSpec]]:
        return (
            ReferenceSpec(
                role=ReferenceRole.CROP_SHEET, image_bytes=sheet, mime_type="image/jpeg"
            ),
            [
                ReferenceSpec(
                    role=ReferenceRole.HUMAN_REF, image_bytes=h, mime_type="image/jpeg"
                )
                for h in humans
            ],
        )

    sheet_spec, human_specs = _build_specs(sheet_for_gemini, humans_for_gemini)
    built = build_sprite_references(sheet_spec, human_specs, object_inputs, req.crops)
    variables = {
        "image_guide": built["guide_text"],
        "cell_swap_plan": built["manifest_vars"]["cell_swap_plan"],
    }
    try:
        rendered_prompt, _model = await load_and_render(SYSTEM_PROMPT_NAME, variables)
    except PromptTemplateNotFound as exc:
        raise RemixDomainError(
            status=500, code="PROMPT_TEMPLATE_NOT_FOUND",
            message=f"Prompt template '{SYSTEM_PROMPT_NAME}' missing — seed not applied?",
        ) from exc

    # 7. Hard-guard total base64 (sheet + humans + prompt < 20MB). Humans were
    #    already pooled small in step 5; the helper shrinks uniformly only if the
    #    combined payload still exceeds the ceiling.
    try:
        sheet_for_gemini, humans_for_gemini, _ = await enforce_total_base64_budget(
            sheet_for_gemini, humans_for_gemini, rendered_prompt,
        )
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR",
            message="Total Gemini payload exceeds 20MB after hard-guard scale",
        ) from exc

    payload_bytes_humans = sum(len(b) for b in humans_for_gemini)

    # 8. Re-build references on the POST-guard bytes → final image parts (atomic
    #    invariant: bytes changed → rebuild). content_parts = [prompt, sheet,
    #    human_1, ..., human_M] — the builder owns the [CROP_SHEET, *HUMAN_REF]
    #    order; the text part is prepended here (so "Ảnh #1" == the first image).
    aspect_ratio = snap_aspect_ratio(sheet_w, sheet_h)
    sheet_spec, human_specs = _build_specs(sheet_for_gemini, humans_for_gemini)
    built = build_sprite_references(sheet_spec, human_specs, object_inputs, req.crops)
    content_parts: list[dict] = [
        {"type": "text", "text": rendered_prompt},
        *built["parts"],
    ]

    message = HumanMessage(content=content_parts)

    # Resolve per-job model knobs (None → hardcoded defaults → parity).
    gemini_id = _PUBLIC_TO_GEMINI.get(req.model) or GEMINI_MODEL_ID
    temperature = req.temperature if req.temperature is not None else GEMINI_TEMPERATURE

    # 9. Gemini call (SHARED concurrency gate with 02/04) via the shared helper.
    gemini_t0 = time.monotonic()
    logger.debug(
        "remix_swap_sprite_gemini_invoke model=%s aspect=%s n_objects=%d images=%d temperature=%s",
        gemini_id, aspect_ratio, n_objects, len(content_parts) - 1, temperature,
    )
    try:
        async with _gemini_sem:
            result = await gemini_ainvoke(
                model=gemini_id,
                messages=[message],
                run_name="remix_swap_sprite_sheet",
                timeout_s=GEMINI_TIMEOUT_S,
                temperature=temperature,
                response_modalities=["IMAGE"],
                image_config={"aspect_ratio": aspect_ratio, "image_size": GEMINI_IMAGE_SIZE},
                ai_context=ai_context,
            )
    except Exception as exc:
        gemini_ms = int((time.monotonic() - gemini_t0) * 1000)
        status, code = classify_gemini_exc(exc)
        if code == "GEMINI_ERROR":
            code = "GEMINI_API_ERROR"
        logger.error(
            "remix_swap_sprite_gemini_error gemini_ms=%d status=%d code=%s err_type=%s err=%s",
            gemini_ms, status, code, type(exc).__name__, str(exc)[:300],
        )
        raise RemixDomainError(status=status, code=code, message=str(exc)[:200]) from exc

    gemini_ms = int((time.monotonic() - gemini_t0) * 1000)
    response = result.message
    token_usage: Optional[int] = result.total_tokens
    ai_request_id = result.ai_request_id
    logger.info(
        "remix_swap_sprite_gemini_ok gemini_ms=%d tokens=%s", gemini_ms, token_usage,
    )

    # 10. finish_reason BEFORE extraction (safety block masquerades as NO_IMAGE).
    finish_reason = _finish_reason(response)
    if finish_reason in _SAFETY_FINISH_REASONS:
        logger.warning("remix_swap_sprite_safety_block finish_reason=%s", finish_reason)
        raise RemixDomainError(
            status=422, code="SAFETY_FILTER_BLOCKED",
            message="Gemini blocked the sprite swap (content/identity policy)",
            details={"finish_reason": finish_reason},
        )

    # 11. Extract image.
    try:
        image_bytes, mime = extract_image(response.content)
    except GeminiResponseError as exc:
        code = "NO_IMAGE_IN_RESPONSE" if exc.code == "NO_IMAGE_RESPONSE" else exc.code
        logger.warning(
            "remix_swap_sprite_no_image finish_reason=%s content_type=%s",
            finish_reason, type(response.content).__name__,
        )
        raise RemixDomainError(
            status=exc.status, code=code, message=exc.message,
            details={"finish_reason": finish_reason},
        ) from exc

    # 12. Ensure PNG at Gemini-native dim — NO resize back to sheet_geometry.
    #     The caller (job 02) rescales geometry to actual (output_w, output_h)
    #     when cutting. Sync callers handle the dim mismatch themselves.
    try:
        final_bytes, output_w, output_h = await asyncio.to_thread(
            ensure_png_native, image_bytes
        )
    except Exception as exc:
        logger.error(
            "remix_swap_sprite_encode_fail src_mime=%s err_type=%s err=%s",
            mime, type(exc).__name__, str(exc)[:200],
        )
        raise RemixDomainError(
            status=502, code="GEMINI_API_ERROR",
            message="Failed to normalize Gemini output to PNG",
        ) from exc

    swapped_object_keys = [o.object_key for o in object_inputs]

    # 12.5. Bytes-mode short-circuit: in-process pipeline skips Storage upload
    #       and returns raw PNG bytes for direct consumption (job 02 cut — avoids
    #       orphan upload + 10MB fetch cap roundtrip on Gemini-native 4K sheets).
    #       composed_sheet_url is independent and still populated above if
    #       return_composed_sheet=True.
    if req.return_bytes:
        logger.info(
            "remix_swap_sprite_done_bytes mode=bytes compose_ms=%d gemini_ms=%d upload_ms=%d objects=%d cells=%d output_w=%d output_h=%d bytes=%d tokens=%s",
            compose_ms, gemini_ms, upload_ms, n_objects, n_crops,
            output_w, output_h, len(final_bytes), token_usage,
        )
        return SwapSpriteSheetCoreResult(
            width=output_w,
            height=output_h,
            token_usage=token_usage,
            composed_sheet_url=composed_sheet_url,
            compose_ms=compose_ms,
            gemini_ms=gemini_ms,
            upload_ms=upload_ms,
            payload_bytes_sheet=len(sheet_for_gemini),
            payload_bytes_humans=payload_bytes_humans,
            object_count=n_objects,
            cell_count=n_crops,
            swapped_object_keys=swapped_object_keys,
            image_url=None,
            image_bytes=final_bytes,
            ai_request_id=ai_request_id,
        )

    # 13. Upload final (URL mode).
    swap_path = build_dated_path(STORAGE_SWAP_PREFIX)
    up_t0 = time.monotonic()
    try:
        final_url = await upload_bytes(swap_path, final_bytes, content_type="image/png")
    except StorageUploadError as exc:
        raise RemixDomainError(
            status=500, code="STORAGE_UPLOAD_ERROR",
            message="Failed to upload sprite swap output",
            details={"path": swap_path},
        ) from exc
    upload_ms += int((time.monotonic() - up_t0) * 1000)

    logger.info(
        "remix_swap_sprite_done mode=url compose_ms=%d gemini_ms=%d upload_ms=%d objects=%d cells=%d output_w=%d output_h=%d tokens=%s",
        compose_ms, gemini_ms, upload_ms, n_objects, n_crops,
        output_w, output_h, token_usage,
    )

    return SwapSpriteSheetCoreResult(
        width=output_w,
        height=output_h,
        token_usage=token_usage,
        composed_sheet_url=composed_sheet_url,
        compose_ms=compose_ms,
        gemini_ms=gemini_ms,
        upload_ms=upload_ms,
        payload_bytes_sheet=len(sheet_for_gemini),
        payload_bytes_humans=payload_bytes_humans,
        object_count=n_objects,
        cell_count=n_crops,
        swapped_object_keys=swapped_object_keys,
        image_url=final_url,
        image_bytes=None,
        ai_request_id=ai_request_id,
    )
