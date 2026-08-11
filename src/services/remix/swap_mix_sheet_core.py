"""Service core for POST /api/remix/swap-mix-crop-sheet.

Multi-target full-identity crop-sheet swap (⚡rev6 2026-06-11 — variant-sheet
input; N ≤ 10, N=1 is the degenerate single-target case). Generic stateless
orchestration:
  compose crop sheet → upload composed (optional) → fit sheet budget → fetch N
  references (FATAL on any failure) + N target_base locators in parallel (FATAL
  when N≥2, skip when N==1) → compute ONE shared variant-sheet layout → compose
  the 2 MIRRORED variant sheets (old + new) → fit each to the variant budget →
  optional upload (variant_sheet_urls) → build prompt (image_guide +
  variant_manifest + crop_manifest) → 3-tier hard-guard (old → new → crop
  sheet) → Gemini call (SHARED `_gemini_sem`) → finish_reason safety check →
  ensure PNG (NO resize — rev5) → upload → result.

⚡rev6: Gemini receives a FIXED 3 images (2 when N=1 without a base) —
  [prompt, crop_sheet, old_variant_sheet?, new_variant_sheet]
Cell i on both variant sheets = target i (mirror invariant — both sheets
compose from the SAME layout). The per-target index bookkeeping of the old
2N-image contract is gone; figure↔appearance mapping rides the baked cell
ordinals + the `variant_manifest` prompt variable.

rev5 (2026-05-28): output is returned at Gemini-native dim (~2K, ≠
`sheet_geometry` in general) — the mix-swap JOB handler runs the post-swap
pipeline; sync callers handle the dim mismatch themselves.

Every failure path maps to `RemixDomainError` with the spec error code; the
router does not catch anything specific. PII discipline: never log/echo URLs,
bytes, or base64 — error details carry only `target_key` / `which` /
`finish_reason`.

Tracing: `@traceable(name="remix_swap_mix_sheet")` — counts/dims only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from langchain_core.messages import HumanMessage
from langsmith import traceable

from src.models.gemini_image_models import (
    GEMINI_IMAGE_MODEL_ID,
    PUBLIC_TO_GEMINI_IMAGE,
)
from src.models.requests.build_crop_sheet import BuildCropSheetRequest, FrameStyle
from src.models.requests.swap_mix_crop_sheet import SwapMixSheetCoreRequest
from src.services import image_ops  # noqa: F401 — Pillow setup side-effects
from src.services.gemini.payload_budget import (
    MAX_MIX_SHEET_BYTES,
    MAX_VARIANT_SHEET_BYTES,
    BudgetExceededError,
    enforce_variant_base64_budget,
    fit_to_budget,
)
from src.services.gemini.response import (
    GeminiResponseError,
    classify_gemini_exc,
    extract_image,
)
from src.services.prompt_loader import PromptTemplateNotFound, load_and_render
from src.services.reference_prompt_builder import ReferenceRole, ReferenceSpec
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
from src.services.remix.swap_mix_prompt_builder import (
    MixCropInput,
    build_mix_references,
)
from src.services.remix.variant_sheet_composer import (
    VariantCellDecodeError,
    compose_variant_sheet,
    compute_variant_sheet_layout,
)
from src.services.ai_usage import AiCallContext
from src.services.gemini.invoke import gemini_ainvoke
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)


# --- Constants ------------------------------------------------------------

SYSTEM_PROMPT_NAME = "SWAP_MIX_CROP_SHEET_SYSTEM"
# Default Gemini `run_name` → becomes `ai_service_logs.operation` at the choke
# point (gemini_ainvoke). Callers MAY override via `run_swap_mix_sheet(run_name=)`
# to re-bucket the cost (e.g. the actor-swap job passes "actor.swap" so the row
# bills the actor swap bucket instead of remix "Generate").
DEFAULT_SWAP_RUN_NAME = "remix_swap_mix_sheet"
# Shared app-wide Gemini image identity (src/models/gemini_image_models.py).
GEMINI_MODEL_ID = GEMINI_IMAGE_MODEL_ID
GEMINI_TEMPERATURE = 0.25  # low — faithful re-draw, less invented detail (parity with sprite-swap)
# D1 (model_params wiring): public→provider map lives in the core (registry is
# provider-agnostic). `req.model` is the public allowlist id; resolve to the
# Gemini provider id here. Unknown/None → GEMINI_MODEL_ID default (parity).
_PUBLIC_TO_GEMINI: dict[str, str] = PUBLIC_TO_GEMINI_IMAGE
GEMINI_TIMEOUT_S = 150.0
GEMINI_IMAGE_SIZE = "4K"

STORAGE_SWAP_PREFIX = "crop-sheet-swaps"
STORAGE_COMPOSED_PREFIX = "crop-sheet-composed"
STORAGE_VARIANT_PREFIX = "variant-sheets"  # ⚡rev6 debug upload (return_composed_sheet)

# Per-image source fetch cap (decompression-bomb guard). References AND
# target_base images land in 768px variant-sheet cells (fit-contain), so no
# per-image pre-shrink step remains — the composer is the natural shrink.
REFERENCE_FETCH_MAX_BYTES = AUX_FETCH_MAX_BYTES  # 10MB


@dataclasses.dataclass(slots=True, frozen=True)
class SwapMixSheetCoreResult:
    width: int
    height: int
    token_usage: Optional[int]
    composed_sheet_url: Optional[str]
    compose_ms: int
    gemini_ms: int
    upload_ms: int
    # ⚡rev6 per-image payload observability (spec meta geminiPayloadBytes).
    payload_bytes_sheet: int
    payload_bytes_variant_old: Optional[int]
    payload_bytes_variant_new: int
    target_count: int
    targets_with_base: int
    skipped_references: list[dict]
    # ⚡rev6 — only when return_composed_sheet=True: {'old'?: str, 'new': str}.
    variant_sheet_urls: Optional[dict] = None
    # Optional with defaults — image_url populated in URL-mode, image_bytes in bytes-mode.
    # `repr=False` on bytes to avoid leaking PNG payload in logs/error messages.
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = dataclasses.field(default=None, repr=False)
    # AI-usage contract (Phase 05): `ai_service_logs.id` of the Gemini swap call.
    ai_request_id: str = ""


# --- Internal helpers -----------------------------------------------------


def _build_compose_request(req: SwapMixSheetCoreRequest) -> BuildCropSheetRequest:
    """Project the mix swap request into the sibling composer's contract."""
    return BuildCropSheetRequest(
        sheet_geometry=req.sheet_geometry,
        crops=req.crops,
        frame=FrameStyle(),  # composer fills its mapping-constant defaults
        response_format="base64",  # unused by compose_crop_sheet (returns bytes)
    )


def _build_mix_crop_inputs(crops: list) -> list[MixCropInput]:
    """Bundle each request crop with its per-cell object roster (Validation S1
    Q1). Roster source: the first-class `crop.objects` (jobs handler sets it);
    falls back to a legacy `annotation["objects"]` (sync caller) so neither path
    loses the roster. `MixCropInput` carries it as a first-class field → the
    builder renders `crop_manifest[].objects` without reading the annotation."""
    out: list[MixCropInput] = []
    for c in crops:
        roster = list(c.objects) if getattr(c, "objects", None) else None
        if roster is None and isinstance(getattr(c, "annotation", None), dict):
            legacy = c.annotation.get("objects")
            if isinstance(legacy, list) and legacy:
                roster = list(legacy)
        out.append(MixCropInput(crop=c, objects=roster or []))
    return out


def _variant_sheet_path(label: str, pair_id: str) -> str:
    """`variant-sheets/{yyyy-mm-dd}/{uuid}-{old|new}.png` — shared `pair_id`
    keeps the 2 debug sheets visually adjacent in Storage."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{STORAGE_VARIANT_PREFIX}/{date}/{pair_id}-{label}.png"


# --- Public API -----------------------------------------------------------


@traceable(name="remix_swap_mix_sheet")
async def run_swap_mix_sheet(
    req: SwapMixSheetCoreRequest,
    *,
    ai_context: AiCallContext | None = None,
    run_name: str | None = None,
) -> SwapMixSheetCoreResult:
    """End-to-end multi-target AI pipeline. Generic — no DB read/write.

    `ai_context` (Phase 05) attributes the Gemini swap call: the sync endpoint
    passes `AiCallContext()` (remix_id absent from the body), the mix-swap job
    (05) passes the job-row context (remix_id → separate remix billing bucket).

    `run_name` (optional) overrides the Gemini call's `run_name`, which the choke
    point (`gemini_ainvoke`) records verbatim as `ai_service_logs.operation`.
    None → `DEFAULT_SWAP_RUN_NAME` ("remix_swap_mix_sheet") so the remix path is
    unchanged; the actor-swap job passes "actor.swap" to bill the swap bucket.
    """
    effective_run_name = run_name or DEFAULT_SWAP_RUN_NAME
    n_crops = len(req.crops)
    n_targets = len(req.swap_targets)
    sheet_w = req.sheet_geometry.width
    sheet_h = req.sheet_geometry.height

    logger.info(
        "remix_swap_mix_start n_crops=%d n_targets=%d sheet=%dx%d return_composed=%s",
        n_crops, n_targets, sheet_w, sheet_h, req.return_composed_sheet,
    )

    # 1. Compose sheet (reuse sibling). ALL_CROPS_FAILED (422) bubbles up.
    compose_t0 = time.monotonic()
    composed = await compose_crop_sheet(_build_compose_request(req))
    compose_ms = int((time.monotonic() - compose_t0) * 1000)
    logger.debug(
        "remix_swap_mix_composed composed=%d skipped=%d sheet_bytes=%d compose_ms=%d",
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

    # 3. Fit composed sheet to the mix sheet budget (6MB).
    try:
        sheet_for_gemini = await fit_to_budget(composed.png_bytes, MAX_MIX_SHEET_BYTES)
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR",
            message="Composed sheet exceeds Gemini sheet budget",
        ) from exc

    # 4. Fetch N references in parallel (FATAL on ANY failure — each is a
    #    target's new appearance; a missing cell would teach the model a wrong
    #    mapping). First failing index wins the error attribution.
    ref_results = await asyncio.gather(
        *[
            _fetch_one(t.reference_image_url, "reference", REFERENCE_FETCH_MAX_BYTES)
            for t in req.swap_targets
        ],
        return_exceptions=True,
    )
    raw_refs: list[bytes] = []
    for i, res in enumerate(ref_results):
        if isinstance(res, BaseException):
            t_key = req.swap_targets[i].key
            inner = res.code if isinstance(res, RemixDomainError) else type(res).__name__
            logger.warning(
                "remix_swap_mix_reference_fatal target_key=%s inner=%s", t_key, inner
            )
            raise RemixDomainError(
                status=422, code="REFERENCE_FETCH_ERROR",
                message="reference image fetch/decode failed (target appearance missing)",
                details={"target_key": t_key, "inner_code": inner},
            ) from (res if isinstance(res, Exception) else None)
        raw_refs.append(res)

    # 5. Fetch N target_base locators in parallel. ⚡rev6: a missing/failed base
    #    when N≥2 breaks the old-sheet mirror (a hole mis-maps EVERY target) →
    #    FATAL. N==1 → non-fatal: drop the old sheet (2-image payload).
    skipped_references: list[dict] = []
    base_bytes: list[bytes] = []
    has_old_sheet = False
    if n_targets >= 2:
        for t in req.swap_targets:
            if not t.target_base_image_url:
                logger.warning(
                    "remix_swap_mix_target_base_missing target_key=%s", t.key
                )
                raise RemixDomainError(
                    status=422, code="TARGET_BASE_FETCH_ERROR",
                    message=(
                        "target_base image missing (locator required for "
                        "multi-target mix — old-variant sheet would have a hole)"
                    ),
                    details={"target_key": t.key, "reason": "MISSING"},
                )
        base_results = await asyncio.gather(
            *[
                _fetch_one(
                    t.target_base_image_url, "target_base", REFERENCE_FETCH_MAX_BYTES
                )
                for t in req.swap_targets
            ],
            return_exceptions=True,
        )
        for i, res in enumerate(base_results):
            if isinstance(res, BaseException):
                t_key = req.swap_targets[i].key
                logger.warning(
                    "remix_swap_mix_target_base_fatal target_key=%s inner=%s",
                    t_key,
                    res.code if isinstance(res, RemixDomainError) else type(res).__name__,
                )
                raise RemixDomainError(
                    status=422, code="TARGET_BASE_FETCH_ERROR",
                    message=(
                        "target_base image fetch/decode failed (locator required "
                        "for multi-target mix)"
                    ),
                    details={"target_key": t_key, "reason": "FETCH_ERROR"},
                ) from (res if isinstance(res, Exception) else None)
            base_bytes.append(res)
        has_old_sheet = True
    else:
        t = req.swap_targets[0]
        if t.target_base_image_url:
            try:
                base_bytes.append(
                    await _fetch_one(
                        t.target_base_image_url, "target_base", REFERENCE_FETCH_MAX_BYTES
                    )
                )
                has_old_sheet = True
            except RemixDomainError:
                skipped_references.append(
                    {"kind": "target_base", "target_key": t.key, "reason": "FETCH_ERROR"}
                )
                logger.warning(
                    "remix_swap_mix_target_base_skipped target_key=%s reason=FETCH_ERROR",
                    t.key,
                )

    # 6. ⚡rev6 — ONE shared layout, then compose the 2 MIRRORED variant sheets.
    #    Decode failures map back onto the owning target (composer raises
    #    VariantCellDecodeError(index)). compose_ms accumulates crop-sheet +
    #    variant-sheet compose time (spec meta composeMs).
    variant_t0 = time.monotonic()
    layout = compute_variant_sheet_layout(n_targets)
    try:
        new_sheet = await compose_variant_sheet(raw_refs, layout)
    except VariantCellDecodeError as exc:
        t_key = req.swap_targets[exc.index].key
        logger.warning(
            "remix_swap_mix_reference_decode_fatal target_key=%s", t_key
        )
        raise RemixDomainError(
            status=422, code="REFERENCE_FETCH_ERROR",
            message="reference image fetch/decode failed (target appearance missing)",
            details={"target_key": t_key, "inner_code": "DECODE_ERROR"},
        ) from exc

    old_sheet: Optional[bytes] = None
    if has_old_sheet:
        try:
            old_sheet = await compose_variant_sheet(base_bytes, layout)
        except VariantCellDecodeError as exc:
            t_key = req.swap_targets[exc.index].key
            if n_targets >= 2:
                logger.warning(
                    "remix_swap_mix_target_base_decode_fatal target_key=%s", t_key
                )
                raise RemixDomainError(
                    status=422, code="TARGET_BASE_FETCH_ERROR",
                    message=(
                        "target_base image fetch/decode failed (locator required "
                        "for multi-target mix)"
                    ),
                    details={"target_key": t_key, "reason": "DECODE_ERROR"},
                ) from exc
            has_old_sheet = False
            skipped_references.append(
                {"kind": "target_base", "target_key": t_key, "reason": "DECODE_ERROR"}
            )
            logger.warning(
                "remix_swap_mix_target_base_skipped target_key=%s reason=DECODE_ERROR",
                t_key,
            )
    targets_with_base = n_targets if has_old_sheet else 0
    compose_ms += int((time.monotonic() - variant_t0) * 1000)

    # 7. Fit each variant sheet to its budget (4MB per sheet).
    try:
        new_sheet = await fit_to_budget(new_sheet, MAX_VARIANT_SHEET_BYTES)
        if old_sheet is not None:
            old_sheet = await fit_to_budget(old_sheet, MAX_VARIANT_SHEET_BYTES)
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR",
            message="Variant sheet exceeds Gemini variant budget",
        ) from exc

    # 8. ⚡rev6 — optional debug upload of BOTH variant sheets (mapping QA).
    variant_sheet_urls: Optional[dict] = None
    if req.return_composed_sheet:
        pair_id = uuid.uuid4().hex
        up_t0 = time.monotonic()
        try:
            new_url = await upload_bytes(
                _variant_sheet_path("new", pair_id), new_sheet,
                content_type="image/png",
            )
            variant_sheet_urls = {"new": new_url}
            if old_sheet is not None:
                variant_sheet_urls["old"] = await upload_bytes(
                    _variant_sheet_path("old", pair_id), old_sheet,
                    content_type="image/png",
                )
        except StorageUploadError as exc:
            raise RemixDomainError(
                status=500, code="STORAGE_UPLOAD_ERROR",
                message="Failed to upload variant sheet",
            ) from exc
        upload_ms += int((time.monotonic() - up_t0) * 1000)

    # 9. Build the atomic references (builder OWNS parts + 2 manifests + guide;
    #    old sheet inferred by PRESENCE-OF-ROLE — `old_spec is None` ⇔ N=1/skip).
    #    Roster bundled per-crop via `MixCropInput` (Validation S1 Q1). Called once
    #    on the pre-guard bytes for the guide/manifest vars (byte-independent →
    #    identical after the step-10 rescale).
    mix_inputs = _build_mix_crop_inputs(req.crops)

    def _build_specs(
        crop_sheet: bytes, old: Optional[bytes], new: bytes
    ) -> tuple[ReferenceSpec, Optional[ReferenceSpec], ReferenceSpec]:
        crop_spec = ReferenceSpec(
            role=ReferenceRole.CROP_SHEET, image_bytes=crop_sheet, mime_type="image/jpeg"
        )
        old_spec = (
            ReferenceSpec(
                role=ReferenceRole.OLD_VARIANT_SHEET, image_bytes=old,
                mime_type="image/jpeg",
            )
            if old is not None
            else None
        )
        new_spec = ReferenceSpec(
            role=ReferenceRole.NEW_VARIANT_SHEET, image_bytes=new, mime_type="image/jpeg"
        )
        return crop_spec, old_spec, new_spec

    crop_spec, old_spec, new_spec = _build_specs(sheet_for_gemini, old_sheet, new_sheet)
    built = build_mix_references(
        crop_spec, new_spec, old_spec, req.swap_targets, layout.cells, mix_inputs
    )
    variables = {
        "image_guide": built["guide_text"],
        "variant_manifest": built["manifest_vars"]["variant_manifest"],
        "crop_manifest": built["manifest_vars"]["crop_manifest"],
    }
    try:
        rendered_prompt, _model = await load_and_render(SYSTEM_PROMPT_NAME, variables)
    except PromptTemplateNotFound as exc:
        raise RemixDomainError(
            status=500, code="PROMPT_TEMPLATE_NOT_FOUND",
            message=f"Prompt template '{SYSTEM_PROMPT_NAME}' missing — seed not applied?",
        ) from exc

    # 10. ⚡rev6 hard-guard (3-tier safety net: old → new → crop sheet).
    try:
        sheet_for_gemini, old_sheet, new_sheet = await enforce_variant_base64_budget(
            sheet_for_gemini, old_sheet, new_sheet, rendered_prompt
        )
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR",
            message="Total Gemini payload exceeds 20MB after variant hard-guard scale",
        ) from exc

    # 11. Re-build references on the POST-guard bytes → final image parts (atomic
    #     invariant: bytes changed → rebuild). content_parts = [prompt, crop_sheet,
    #     old?, new] — the builder owns the [CROP_SHEET, OLD?, NEW] order.
    aspect_ratio = snap_aspect_ratio(sheet_w, sheet_h)
    crop_spec, old_spec, new_spec = _build_specs(sheet_for_gemini, old_sheet, new_sheet)
    built = build_mix_references(
        crop_spec, new_spec, old_spec, req.swap_targets, layout.cells, mix_inputs
    )
    content_parts: list[dict] = [
        {"type": "text", "text": rendered_prompt},
        *built["parts"],
    ]

    message = HumanMessage(content=content_parts)

    # Resolve per-job model knobs (None → hardcoded defaults → parity).
    gemini_id = _PUBLIC_TO_GEMINI.get(req.model) or GEMINI_MODEL_ID
    temperature = req.temperature if req.temperature is not None else GEMINI_TEMPERATURE

    # 12. Gemini call (SHARED concurrency gate) via the shared invoke helper.
    gemini_t0 = time.monotonic()
    logger.debug(
        "remix_swap_mix_gemini_invoke model=%s aspect=%s n_targets=%d with_old_sheet=%s images=%d temperature=%s",
        gemini_id, aspect_ratio, n_targets, has_old_sheet,
        len(content_parts) - 1, temperature,
    )
    try:
        async with _gemini_sem:
            result = await gemini_ainvoke(
                model=gemini_id,
                messages=[message],
                run_name=effective_run_name,
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
            "remix_swap_mix_gemini_error gemini_ms=%d status=%d code=%s err_type=%s err=%s",
            gemini_ms, status, code, type(exc).__name__, str(exc)[:300],
        )
        raise RemixDomainError(status=status, code=code, message=str(exc)[:200]) from exc

    gemini_ms = int((time.monotonic() - gemini_t0) * 1000)
    response = result.message
    token_usage: Optional[int] = result.total_tokens
    ai_request_id = result.ai_request_id
    logger.info("remix_swap_mix_gemini_ok gemini_ms=%d tokens=%s", gemini_ms, token_usage)

    # 13. finish_reason BEFORE extraction (safety block masquerades as NO_IMAGE).
    finish_reason = _finish_reason(response)
    if finish_reason in _SAFETY_FINISH_REASONS:
        logger.warning("remix_swap_mix_safety_block finish_reason=%s", finish_reason)
        raise RemixDomainError(
            status=422, code="SAFETY_FILTER_BLOCKED",
            message="Gemini blocked the mix swap (content/identity policy)",
            details={"finish_reason": finish_reason},
        )

    # 14. Extract image.
    try:
        image_bytes, mime = extract_image(response.content)
    except GeminiResponseError as exc:
        code = "NO_IMAGE_IN_RESPONSE" if exc.code == "NO_IMAGE_RESPONSE" else exc.code
        logger.warning(
            "remix_swap_mix_no_image finish_reason=%s content_type=%s",
            finish_reason, type(response.content).__name__,
        )
        raise RemixDomainError(
            status=exc.status, code=code, message=exc.message,
            details={"finish_reason": finish_reason},
        ) from exc

    # 15. Ensure PNG at Gemini-native dim — NO resize back to sheet_geometry
    #     (rev5 2026-05-28). Job handler runs post-swap pipeline that rescales
    #     geometry to actual (output_w, output_h) and produces the canonical
    #     sheet at sheet_geometry. Sync callers handle dim mismatch themselves.
    try:
        final_bytes, output_w, output_h = await asyncio.to_thread(
            ensure_png_native, image_bytes
        )
    except Exception as exc:
        logger.error(
            "remix_swap_mix_encode_fail src_mime=%s err_type=%s err=%s",
            mime, type(exc).__name__, str(exc)[:200],
        )
        raise RemixDomainError(
            status=502, code="GEMINI_API_ERROR",
            message="Failed to normalize Gemini output to PNG",
        ) from exc

    # 15.5. Bytes-mode short-circuit: in-process pipeline skips Storage upload
    #       and returns raw PNG bytes for direct consumption (e.g. post-swap
    #       pipeline cut — avoids orphan upload + 10 MB fetch cap roundtrip
    #       on Gemini-native 4K sheets). composed_sheet_url / variant_sheet_urls
    #       are independent and still populated above if return_composed_sheet.
    if req.return_bytes:
        logger.info(
            "remix_swap_mix_done_bytes mode=bytes compose_ms=%d gemini_ms=%d upload_ms=%d targets=%d with_base=%d output_w=%d output_h=%d bytes=%d tokens=%s",
            compose_ms, gemini_ms, upload_ms, n_targets, targets_with_base,
            output_w, output_h, len(final_bytes), token_usage,
        )
        return SwapMixSheetCoreResult(
            width=output_w,
            height=output_h,
            token_usage=token_usage,
            composed_sheet_url=composed_sheet_url,
            compose_ms=compose_ms,
            gemini_ms=gemini_ms,
            upload_ms=upload_ms,
            payload_bytes_sheet=len(sheet_for_gemini),
            payload_bytes_variant_old=len(old_sheet) if old_sheet is not None else None,
            payload_bytes_variant_new=len(new_sheet),
            target_count=n_targets,
            targets_with_base=targets_with_base,
            skipped_references=skipped_references,
            variant_sheet_urls=variant_sheet_urls,
            image_url=None,
            image_bytes=final_bytes,
            ai_request_id=ai_request_id,
        )

    # 16. Upload final (URL mode).
    swap_path = build_dated_path(STORAGE_SWAP_PREFIX)
    up_t0 = time.monotonic()
    try:
        final_url = await upload_bytes(swap_path, final_bytes, content_type="image/png")
    except StorageUploadError as exc:
        raise RemixDomainError(
            status=500, code="STORAGE_UPLOAD_ERROR",
            message="Failed to upload mix swap output",
            details={"path": swap_path},
        ) from exc
    upload_ms += int((time.monotonic() - up_t0) * 1000)

    logger.info(
        "remix_swap_mix_done mode=url compose_ms=%d gemini_ms=%d upload_ms=%d targets=%d with_base=%d output_w=%d output_h=%d tokens=%s",
        compose_ms, gemini_ms, upload_ms, n_targets, targets_with_base,
        output_w, output_h, token_usage,
    )

    return SwapMixSheetCoreResult(
        width=output_w,
        height=output_h,
        token_usage=token_usage,
        composed_sheet_url=composed_sheet_url,
        compose_ms=compose_ms,
        gemini_ms=gemini_ms,
        upload_ms=upload_ms,
        payload_bytes_sheet=len(sheet_for_gemini),
        payload_bytes_variant_old=len(old_sheet) if old_sheet is not None else None,
        payload_bytes_variant_new=len(new_sheet),
        target_count=n_targets,
        targets_with_base=targets_with_base,
        skipped_references=skipped_references,
        variant_sheet_urls=variant_sheet_urls,
        image_url=final_url,
        image_bytes=None,
        ai_request_id=ai_request_id,
    )
