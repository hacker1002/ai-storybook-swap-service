"""detect-mix-defects core (`run_detect_mix_defects`) — MIX swap defect localization.

Spec: ai-storybook-design/api/remix/07-detect-mix-defects.md (AUTHORITATIVE).
Sibling of 06 (`detect_swap_defects_core`) for the MIX plane.

Image-IN / defect-regions-OUT. ONE Gemini multimodal call compares the mix swap
RESULT against the reference context that drove swap 04 — the ORIGINAL mix crop
sheet (#1), the OLD-variant locator sheet (#2, omitted when N=1 has no base), the
NEW-variant target sheet (#3) — plus the `variant_manifest` + `crop_manifest` +
`builder_params` text, and returns a free list of defect boxes
(`[ymin,xmin,ymax,xmax]` 0-1000 on the RESULT, the LAST image). The server
converts each box → px circle (center + half-diagonal radius) + keeps the px box,
then filters (focus/severity) → sorts (severity desc, confidence desc) → caps —
all via the SHARED `defect_postprocess` engine (DRY 06↔07).

Why a SEPARATE core from 06? Mix swap is full-identity, N-target: the reference
is 2 VARIANT SHEETS (old locator + new target — illustrations, NOT human photos),
the figure↔identity mapping rides TWO independent number scales (crop-cell ≠
target-cell), each crop cell is multi-subject, and the category set is
mix-adapted (`unrelated_object_changed` replaces `trait_leak`). Shared infra:
`crop_sheet_composer` (×2 ORIGINAL+RESULT), `variant_sheet_composer` (×2 OLD+NEW),
`ai_image_downscale`, `gemini_payload_budget`, `defect_postprocess`, the mix
prompt builder, and the 06 response leaves (`DefectPoint`/`DefectBox`/...).

RESULT (parity 06): RECOMPOSED in-process from `result_crops[]` via the SAME
`compose_crop_sheet` as the ORIGINAL → both sheets share `sheet_geometry`
(`swappedDimensions == sheet_geometry`, pixel-aligned). Every input image is
cost-downscaled before Gemini; the 0-1000→px basis is `sheet_geometry` (measured
BEFORE downscale), so the downscale is lossless to the coordinates.

Advisory / non-fatal: every failure raises `RemixDomainError` (router → spec
envelope); the caller treats ANY error as "couldn't inspect" and keeps the swap
result. An empty `defects` list is SUCCESS (ran + found nothing wrong).

PII discipline (parity 06): NEVER log/echo raw URLs, image bytes, base64,
`object_context.visual_description`, `appearance`, or `defect.message`. Error
details carry only `target_key` (entity key, non-PII).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import time
from typing import Any, Optional

from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from langsmith import traceable

from src.models.requests.build_crop_sheet import BuildCropSheetRequest, FrameStyle
from src.models.requests.detect_mix_defects import (
    DETECT_MIX_DEFECTS_DEFAULT_MODEL,
    DETECT_MIX_DEFECTS_SYSTEM_NAME,
    DETECT_TEMPERATURE,
    DETECT_TIMEOUT_S,
    MAX_DEFECT_MESSAGE_LEN,
    MAX_DETECT_RETRIES,
    MAX_IMAGE_BYTES,
    MAX_ORIGINAL_SHEET_BYTES,
    MIX_DEFECT_CATEGORIES,
    DefectBox,
    DefectPoint,
    DetectMixDefectsMeta,
    DetectMixDefectsRequest,
    SwapDefect,
    SwappedDimensions,
)
from src.services import http_fetch
from src.services.ai_image_downscale import (
    SHEET_AI_MAX_EDGE,
    VARIANT_AI_MAX_EDGE,
    downscale_for_ai_cost,
)
from src.services.gemini.payload_budget import (
    GEMINI_INLINE_LIMIT_BYTES,
    BudgetExceededError,
    compute_base64_size,
    fit_to_budget,
)
from src.services.ai_usage import AiCallContext
from src.services.gemini.invoke import gemini_ainvoke
from src.services.gemini.response import classify_gemini_exc
from src.services.prompt_loader import PromptTemplateNotFound, load_and_render
from src.services.reference_prompt_builder import ReferenceRole, ReferenceSpec
from src.services.remix.crop_sheet_composer import compose_crop_sheet
from src.services.remix.defect_postprocess import (
    map_defects_to_circles as _map_defects_engine,
)
from src.services.remix.errors import RemixDomainError
from src.services.remix.swap_mix_prompt_builder import (
    MixCropInput,
    build_detect_builder_params,
    build_mix_detect_image_guide,
    build_mix_references,
)
from src.services.remix.variant_sheet_composer import (
    VariantCellDecodeError,
    compose_variant_sheet,
    compute_variant_sheet_layout,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFECTS_SCHEMA",
    "DetectMixDefectsResult",
    "map_defects_to_circles",
    "run_detect_mix_defects",
]

_SAFETY_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}

# Separate concurrency pool from the image-preview swap (`_gemini_sem`): detect is
# a cheaper TEXT/vision model (gemini-3.5-flash) on a different rate-limit pool,
# so it must not compete for the image-gen slots. Bounds the +1 flash call/sheet.
_DETECT_CONCURRENCY_CAP = 3
_DETECT_SEM = asyncio.Semaphore(_DETECT_CONCURRENCY_CAP)

_REINFORCE = (
    "\n\nNHẮC LẠI: chỉ trả JSON đúng schema "
    '{ "defects": [ { "box": [ymin,xmin,ymax,xmax], "category": "<optional>", '
    '"severity": "<optional>", "cell": <optional — số ô CROP>, '
    '"object_key": "<optional — target key>", "message": "<optional>", '
    '"confidence": <optional> } ] }. '
    "box hệ 0-1000 trên ẢNH KẾT QUẢ (ảnh CUỐI). 2 thang số ô ĐỘC LẬP "
    "(số Ảnh #1/KẾT QUẢ = ô CROP). KHÔNG thấy lỗi → defects:[]. KHÔNG văn xuôi."
)

# Gemini structured-output schema — defects only. `box` required; everything else
# optional. Server validates + drops bad items.
DEFECTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "defects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "box": {"type": "array", "items": {"type": "integer"}},
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                    "cell": {"type": "integer"},
                    "object_key": {"type": "string"},
                    "message": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["box"],
            },
        }
    },
    "required": ["defects"],
}


# ─── Result ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass(slots=True)
class DetectMixDefectsResult:
    """Core output — the router builds the response envelope from this."""

    defects: list[SwapDefect]
    meta: DetectMixDefectsMeta


# ─── Pure mapping: box → circle + filter + sort + cap (DRY 06↔07) ────────────


def _make_mix_defect(
    *,
    center_x: int,
    center_y: int,
    radius: int,
    box_x: int,
    box_y: int,
    box_w: int,
    box_h: int,
    category: Optional[str],
    severity: Optional[str],
    message: Optional[str],
    confidence: Optional[float],
    cell: Optional[int],
    object_key: Optional[str],
) -> SwapDefect:
    """Factory passed to the shared `defect_postprocess` engine — builds ONE
    MIX-plane `SwapDefect` (category ∈ MixDefectCategory)."""
    return SwapDefect(
        center=DefectPoint(x=center_x, y=center_y),
        radius=radius,
        box=DefectBox(x=box_x, y=box_y, w=box_w, h=box_h),
        category=category,
        severity=severity,
        message=message,
        confidence=confidence,
        cell=cell,
        object_key=object_key,
    )


def map_defects_to_circles(
    raw_defects: list[dict],
    w_s: int,
    h_s: int,
    *,
    focus_objects: Optional[list[str]] = None,
    severity_threshold: Optional[str] = None,
    max_defects: int = 30,
) -> tuple[list[SwapDefect], int, bool]:
    """Convert raw Gemini defects → MIX `SwapDefect[]` on the RESULT image.

    Thin wrapper over the shared `defect_postprocess` engine: injects the
    mix-plane `SwapDefect` factory + the 10 MIX categories + the message cap.
    """
    return _map_defects_engine(
        raw_defects,
        w_s,
        h_s,
        defect_factory=_make_mix_defect,
        categories=MIX_DEFECT_CATEGORIES,
        focus_objects=focus_objects,
        severity_threshold=severity_threshold,
        max_defects=max_defects,
        max_message_len=MAX_DEFECT_MESSAGE_LEN,
    )


# ─── Gemini parse helpers (parity 06 — per-core so the test can patch the
#     `ChatGoogleGenerativeAI` symbol in THIS module's namespace) ─────────────


class _ParseRetry(Exception):
    """Internal — globally-malformed response on the FIRST attempt → retry once."""


def _strip_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _extract_text(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


def _extract_token(response: Any) -> Optional[int]:
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        return total
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    summed = (inp if isinstance(inp, int) else 0) + (out if isinstance(out, int) else 0)
    return summed or None


def _raise_if_safety_blocked(response: Any) -> None:
    meta = getattr(response, "response_metadata", None)
    if not isinstance(meta, dict):
        return
    reason = meta.get("finish_reason") or meta.get("finishReason")
    if isinstance(reason, str) and reason.upper() in _SAFETY_FINISH_REASONS:
        raise RemixDomainError(
            status=422,
            code="SAFETY_FILTER_BLOCKED",
            message="Content blocked by Gemini safety filter",
        )


def _parse_defects(raw_text: str, *, final: bool) -> list[dict]:
    """Tolerant parse → list of raw defect dicts (each carrying at least `box`).

    Accepts `{defects:[...]}` or a bare array. Individually-malformed items are
    dropped at map-time. Globally malformed → `_ParseRetry` on the first attempt,
    `RemixDomainError(500, PARSE_ERROR)` on the final attempt.
    """
    cleaned = _strip_fence(raw_text)

    def _bad(msg: str):
        logger.warning("detect_mix_defects_parse_invalid final=%s cleaned_len=%d", final, len(cleaned))
        if final:
            return RemixDomainError(status=500, code="PARSE_ERROR", message=msg)
        return _ParseRetry(msg)

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise _bad("Gemini returned non-JSON defects") from exc

    items: Any = obj
    if isinstance(obj, dict):
        if isinstance(obj.get("defects"), list):
            items = obj["defects"]
        else:
            raise _bad("Response object has no defects array")
    if not isinstance(items, list):
        raise _bad("Expected a JSON array of defects")

    return [it for it in items if isinstance(it, dict)]


def _img_part(data: bytes, mime: str) -> dict:
    b64 = base64.b64encode(data).decode("ascii")
    safe_mime = mime if mime and mime.startswith("image/") else "image/png"
    return {"type": "image_url", "image_url": f"data:{safe_mime};base64,{b64}"}


async def _invoke_gemini(
    model: str,
    content_parts: list[dict],
    *,
    ai_context: AiCallContext | None = None,
) -> Any:
    """One Gemini structured call → the raw `AIMessage`. Maps transport/timeout/
    safety exc to `RemixDomainError` (502 LLM_ERROR / 422 safety)."""
    try:
        async with _DETECT_SEM:
            result = await gemini_ainvoke(
                model=model,
                messages=[HumanMessage(content=content_parts)],
                run_name="detect-mix-defects",
                timeout_s=DETECT_TIMEOUT_S,
                temperature=DETECT_TEMPERATURE,
                response_mime_type="application/json",
                response_schema=DEFECTS_SCHEMA,
                max_retries=MAX_DETECT_RETRIES,
                ai_context=ai_context,
            )
        return result.message
    except RemixDomainError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise RemixDomainError(status=502, code="LLM_ERROR", message="Gemini request timed out") from exc
    except Exception as exc:  # noqa: BLE001
        status, code = classify_gemini_exc(exc)
        if code == "SAFETY_FILTER_BLOCKED":
            raise RemixDomainError(status=422, code="SAFETY_FILTER_BLOCKED", message="Gemini safety block") from exc
        logger.warning("detect_mix_defects_gemini_error code=%s exc_type=%s", code, type(exc).__name__)
        raise RemixDomainError(status=502, code="LLM_ERROR", message="Gemini request failed") from exc


async def _run_detect_gemini(
    rendered_prompt: str,
    image_parts: list[dict],
    model: str,
    *,
    ai_context: AiCallContext | None = None,
) -> tuple[list[dict], Optional[int]]:
    """Gemini defect localization. `image_parts` = [orig, old?, new, result] in
    that contract order (result is the LAST/inspection image). 1 app-level
    parse-retry with reinforcement; langchain `max_retries` covers transient
    transport. Raises `RemixDomainError` on safety/LLM/parse failure. `ai_context`
    (Phase 05) attributes each Gemini call."""
    resp = await _invoke_gemini(
        model, [{"type": "text", "text": rendered_prompt}, *image_parts],
        ai_context=ai_context,
    )
    _raise_if_safety_blocked(resp)
    token = _extract_token(resp)
    text = _extract_text(resp.content)
    if text.strip():
        try:
            return _parse_defects(text, final=False), token
        except _ParseRetry:
            pass

    # Retry once with reinforcement.
    resp2 = await _invoke_gemini(
        model, [{"type": "text", "text": rendered_prompt + _REINFORCE}, *image_parts],
        ai_context=ai_context,
    )
    _raise_if_safety_blocked(resp2)
    token2 = _extract_token(resp2)
    text2 = _extract_text(resp2.content)
    if not text2.strip():
        raise RemixDomainError(status=500, code="PARSE_ERROR", message="Gemini returned empty defects")
    defects = _parse_defects(text2, final=True)
    return defects, (token or 0) + (token2 or 0) or None


# ─── Image prep (SSRF-guarded) ───────────────────────────────────────────────


def _http_exc_code(exc: HTTPException) -> Optional[str]:
    detail = exc.detail
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, str):
                return code
    return None


async def _fetch_image(url: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """SSRF-guarded fetch → bytes. SSRF block → 400 SSRF_BLOCKED (spec envelope);
    any other failure → 422 IMAGE_FETCH_ERROR. No URL logged."""
    try:
        data, _ct = await http_fetch.fetch_image_bytes(url, max_bytes=max_bytes, timeout_s=30.0)
        return data
    except RemixDomainError:
        raise
    except HTTPException as exc:
        if _http_exc_code(exc) == "SSRF_BLOCKED":
            raise RemixDomainError(
                status=400, code="SSRF_BLOCKED", message="URL blocked by SSRF guard"
            ) from exc
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Failed to fetch image"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Failed to fetch image"
        ) from exc


async def _prepare_original(
    req: DetectMixDefectsRequest, original_bytes: Optional[bytes]
) -> bytes:
    """ORIGINAL mix sheet: pre-fetched → use; `original_sheet_url` → fetch (large
    DoS bound); else compose from `crops[]` (ALL_CROPS_FAILED bubbles from the
    composer)."""
    if original_bytes is not None:
        return original_bytes
    if req.original_sheet_url:
        return await _fetch_image(req.original_sheet_url, max_bytes=MAX_ORIGINAL_SHEET_BYTES)
    composed = await compose_crop_sheet(
        BuildCropSheetRequest(
            sheet_geometry=req.sheet_geometry,
            crops=req.crops,
            frame=FrameStyle(),  # composer defaults: ordinal badge ON
            response_format="base64",
        )
    )
    return composed.png_bytes


async def _prepare_result(
    req: DetectMixDefectsRequest, result_bytes: Optional[bytes]
) -> bytes:
    """RESULT mix sheet: pre-composed bytes (test / in-process caller) → use;
    else RECOMPOSE from `result_crops[]` via the SAME composer as the ORIGINAL
    (→ pixel-aligned). The composer's GENERIC `ALL_CROPS_FAILED` (every result
    piece failed) is re-mapped to `ALL_RESULT_CROPS_FAILED` so the caller can
    tell a RESULT-side total failure from an ORIGINAL-side one."""
    if result_bytes is not None:
        return result_bytes
    try:
        composed = await compose_crop_sheet(
            BuildCropSheetRequest(
                sheet_geometry=req.sheet_geometry,
                crops=req.result_crops,
                frame=FrameStyle(),  # SAME defaults as ORIGINAL → pixel-aligned
                response_format="base64",
            )
        )
    except RemixDomainError as exc:
        if exc.code == "ALL_CROPS_FAILED":
            raise RemixDomainError(
                status=422,
                code="ALL_RESULT_CROPS_FAILED",
                message="All result_crops failed to fetch/decode",
                details=exc.details,
            ) from exc
        raise
    return composed.png_bytes


async def _gather_references(req: DetectMixDefectsRequest) -> list[bytes]:
    """Fetch the N NEW reference images (per-target appearance) — FATAL on ANY
    failure (a missing NEW cell = can't judge that target). SSRF stays 400; any
    other failure → 422 REFERENCE_FETCH_ERROR (details.target_key). First failing
    INDEX wins attribution (deterministic)."""
    results = await asyncio.gather(
        *[_fetch_image(t.reference_image_url) for t in req.swap_targets],
        return_exceptions=True,
    )
    raw: list[bytes] = []
    for i, res in enumerate(results):
        if isinstance(res, BaseException):
            t_key = req.swap_targets[i].key
            if isinstance(res, RemixDomainError) and res.code == "SSRF_BLOCKED":
                raise res
            logger.warning("detect_mix_defects_reference_fatal target_key=%s", t_key)
            raise RemixDomainError(
                status=422, code="REFERENCE_FETCH_ERROR",
                message="reference image fetch/decode failed (target appearance missing)",
                details={"target_key": t_key},
            ) from (res if isinstance(res, Exception) else None)
        raw.append(res)
    return raw


async def _gather_target_bases(
    req: DetectMixDefectsRequest,
) -> tuple[list[bytes], bool, list[dict]]:
    """Fetch the N OLD target_base locators. ⚡N-aware: N≥2 → every target MUST
    have a base, any miss/fail = mirror break = FATAL `IMAGE_FETCH_ERROR`; N==1 →
    non-fatal (drop the old sheet → hasOldVariantSheet=False). SSRF on a REQUIRED
    base (N≥2) stays 400; on the OPTIONAL N=1 base it is treated as a skip
    (parity swap 04). Returns `(base_bytes, has_old_sheet, skipped[])`."""
    n = len(req.swap_targets)
    skipped: list[dict] = []
    if n >= 2:
        for t in req.swap_targets:
            if not t.target_base_image_url:
                logger.warning("detect_mix_defects_target_base_missing target_key=%s", t.key)
                raise RemixDomainError(
                    status=422, code="IMAGE_FETCH_ERROR",
                    message="target_base missing (locator required for multi-target mix)",
                    details={"target_key": t.key, "reason": "MISSING"},
                )
        results = await asyncio.gather(
            *[_fetch_image(t.target_base_image_url) for t in req.swap_targets],  # type: ignore[arg-type]
            return_exceptions=True,
        )
        bases: list[bytes] = []
        for i, res in enumerate(results):
            if isinstance(res, BaseException):
                t_key = req.swap_targets[i].key
                if isinstance(res, RemixDomainError) and res.code == "SSRF_BLOCKED":
                    raise res
                logger.warning("detect_mix_defects_target_base_fatal target_key=%s", t_key)
                raise RemixDomainError(
                    status=422, code="IMAGE_FETCH_ERROR",
                    message="target_base fetch/decode failed (locator required for multi-target mix)",
                    details={"target_key": t_key},
                ) from (res if isinstance(res, Exception) else None)
            bases.append(res)
        return bases, True, skipped

    # N == 1 — optional base, non-fatal skip on any failure (incl. SSRF — parity 04).
    t = req.swap_targets[0]
    if t.target_base_image_url:
        try:
            data = await _fetch_image(t.target_base_image_url)
            return [data], True, skipped
        except RemixDomainError:
            skipped.append({"kind": "target_base", "target_key": t.key, "reason": "FETCH_ERROR"})
            logger.warning("detect_mix_defects_target_base_skipped target_key=%s", t.key)
    return [], False, skipped


def _build_mix_crop_inputs(crops: list) -> list[MixCropInput]:
    """Bundle each crop with its per-cell object roster (parity swap 04). Roster
    source: first-class `crop.objects` (jobs handler), else legacy
    `annotation['objects']` (sync caller). The builder renders
    `crop_manifest[].objects` from this — never reads the annotation back out."""
    out: list[MixCropInput] = []
    for c in crops:
        roster = list(c.objects) if getattr(c, "objects", None) else None
        if roster is None and isinstance(getattr(c, "annotation", None), dict):
            legacy = c.annotation.get("objects")
            if isinstance(legacy, list) and legacy:
                roster = list(legacy)
        out.append(MixCropInput(crop=c, objects=roster or []))
    return out


async def _fit_mix_detect_payload(
    orig: bytes, old: Optional[bytes], new: bytes, result: bytes, prompt: str
) -> tuple[bytes, Optional[bytes], bytes, bytes]:
    """Hard-guard the inline payload while keeping the RESULT image sharpest.

    Priority (spec 07): shrink OLD → NEW → ORIG; keep RESULT untouched (the
    inspection target) until a last-resort tier. With the static caps this almost
    never fires — pure safety net. Returns the (possibly shrunk) tuple."""
    cap = GEMINI_INLINE_LIMIT_BYTES
    prompt_b64 = len(prompt.encode("utf-8"))
    margin = 1024

    def total() -> int:
        s = (
            compute_base64_size(len(orig))
            + compute_base64_size(len(new))
            + compute_base64_size(len(result))
            + prompt_b64
        )
        if old is not None:
            s += compute_base64_size(len(old))
        return s

    if total() + margin <= cap:
        return orig, old, new, result

    def _raw_budget(reserved_b64: int) -> int:
        avail_b64 = cap - reserved_b64 - margin
        return max(256 * 1024, avail_b64 * 3 // 4)

    # Tier 1 — shrink OLD (keep orig + new + result).
    if old is not None:
        reserved = (
            compute_base64_size(len(orig))
            + compute_base64_size(len(new))
            + compute_base64_size(len(result))
            + prompt_b64
        )
        try:
            old = await fit_to_budget(old, _raw_budget(reserved))
        except BudgetExceededError:
            pass
        if total() + margin <= cap:
            return orig, old, new, result

    # Tier 2 — shrink NEW.
    reserved = (
        compute_base64_size(len(orig))
        + (compute_base64_size(len(old)) if old is not None else 0)
        + compute_base64_size(len(result))
        + prompt_b64
    )
    try:
        new = await fit_to_budget(new, _raw_budget(reserved))
    except BudgetExceededError:
        pass
    if total() + margin <= cap:
        return orig, old, new, result

    # Tier 3 — shrink ORIG (crop sheet — layout-critical, shrunk before RESULT).
    reserved = (
        (compute_base64_size(len(old)) if old is not None else 0)
        + compute_base64_size(len(new))
        + compute_base64_size(len(result))
        + prompt_b64
    )
    try:
        orig = await fit_to_budget(orig, _raw_budget(reserved))
    except BudgetExceededError:
        pass
    if total() + margin <= cap:
        return orig, old, new, result

    # Tier 4 (pathological) — last resort, shrink the RESULT too.
    reserved = (
        compute_base64_size(len(orig))
        + (compute_base64_size(len(old)) if old is not None else 0)
        + compute_base64_size(len(new))
        + prompt_b64
    )
    try:
        result = await fit_to_budget(result, _raw_budget(reserved))
    except BudgetExceededError as exc:
        raise RemixDomainError(
            status=500, code="INTERNAL",
            message="Detect payload exceeds Gemini inline budget after hard-guard",
        ) from exc
    if total() + margin > cap:
        raise RemixDomainError(
            status=500, code="INTERNAL",
            message="Detect payload exceeds Gemini inline budget after hard-guard",
        )
    return orig, old, new, result


def _build_specs(
    orig: bytes, old: Optional[bytes], new: bytes
) -> tuple[ReferenceSpec, ReferenceSpec, Optional[ReferenceSpec]]:
    """`build_mix_references` arg order: (crop_sheet, new_variant, old_variant).
    All PNG after `downscale_for_ai_cost(reencode='png')` — keeps gutter/grid/
    ordinals crisp for pixel-aligned comparison + preserves alpha."""
    crop_spec = ReferenceSpec(
        role=ReferenceRole.CROP_SHEET, image_bytes=orig, mime_type="image/png"
    )
    new_spec = ReferenceSpec(
        role=ReferenceRole.NEW_VARIANT_SHEET, image_bytes=new, mime_type="image/png"
    )
    old_spec = (
        ReferenceSpec(
            role=ReferenceRole.OLD_VARIANT_SHEET, image_bytes=old, mime_type="image/png"
        )
        if old is not None
        else None
    )
    return crop_spec, new_spec, old_spec


# ─── Core ────────────────────────────────────────────────────────────────────


@traceable(name="remix_detect_mix_defects")
async def run_detect_mix_defects(
    req: DetectMixDefectsRequest,
    *,
    result_bytes: Optional[bytes] = None,
    original_bytes: Optional[bytes] = None,
    ai_context: AiCallContext | None = None,
) -> DetectMixDefectsResult:
    """detect-mix-defects core. Optional pre-composed bytes let a test or
    in-process caller skip the recompose/re-fetch (Phase 02 job 12 calls
    `run_detect_mix_defects(req)` per sheet). `result_bytes` short-circuits the
    `result_crops[]` recompose; `original_bytes` the ORIGINAL compose/fetch."""
    t0 = time.monotonic()
    n_crops = len(req.crops)
    n_targets = len(req.swap_targets)
    w_s = req.sheet_geometry.width
    h_s = req.sheet_geometry.height

    # 1. Prepare images IN PARALLEL, all SSRF-guarded:
    #      RESULT   = recompose `result_crops[]`  (ALL_RESULT_CROPS_FAILED)
    #      ORIGINAL = fast-path url OR compose `crops[]` (ALL_CROPS_FAILED)
    #      refs     = N NEW references (REFERENCE_FETCH_ERROR — fatal)
    #      bases    = N OLD target_base locators (N≥2 fatal / N=1 skip)
    #    Coordinate basis = sheet_geometry (taken HERE, before the downscale below).
    (result_raw, orig_raw, raw_refs, (base_bytes, has_old, skipped_refs)) = (
        await asyncio.gather(
            _prepare_result(req, result_bytes),
            _prepare_original(req, original_bytes),
            _gather_references(req),
            _gather_target_bases(req),
        )
    )

    # 2. Compose the 2 MIRRORED variant sheets from ONE shared layout (mirror
    #    invariant by construction). Decode failures map back onto the target.
    layout = compute_variant_sheet_layout(n_targets)
    try:
        new_sheet = await compose_variant_sheet(raw_refs, layout)
    except VariantCellDecodeError as exc:
        t_key = req.swap_targets[exc.index].key
        raise RemixDomainError(
            status=422, code="REFERENCE_FETCH_ERROR",
            message="reference image fetch/decode failed (target appearance missing)",
            details={"target_key": t_key, "reason": "DECODE_ERROR"},
        ) from exc

    old_sheet: Optional[bytes] = None
    if has_old:
        try:
            old_sheet = await compose_variant_sheet(base_bytes, layout)
        except VariantCellDecodeError as exc:
            t_key = req.swap_targets[exc.index].key
            if n_targets >= 2:
                raise RemixDomainError(
                    status=422, code="IMAGE_FETCH_ERROR",
                    message="target_base fetch/decode failed (locator required for multi-target mix)",
                    details={"target_key": t_key, "reason": "DECODE_ERROR"},
                ) from exc
            has_old = False
            skipped_refs.append({"kind": "target_base", "target_key": t_key, "reason": "DECODE_ERROR"})

    # 3. Cost-downscale every input for Gemini (CPU-bound → to_thread). BOTH sheets
    #    use the SAME SHEET_AI_MAX_EDGE → SAME out dims → stay pixel-aligned;
    #    variant sheets use VARIANT_AI_MAX_EDGE. Coordinates UNAFFECTED (0-1000→px
    #    uses sheet_geometry). `reencode='png'` keeps flat regions crisp.
    ds_tasks = [
        asyncio.to_thread(downscale_for_ai_cost, orig_raw, max_edge=SHEET_AI_MAX_EDGE, reencode="png"),
        asyncio.to_thread(downscale_for_ai_cost, result_raw, max_edge=SHEET_AI_MAX_EDGE, reencode="png"),
        asyncio.to_thread(downscale_for_ai_cost, new_sheet, max_edge=VARIANT_AI_MAX_EDGE, reencode="png"),
    ]
    if has_old and old_sheet is not None:
        ds_tasks.append(
            asyncio.to_thread(downscale_for_ai_cost, old_sheet, max_edge=VARIANT_AI_MAX_EDGE, reencode="png")
        )
    try:
        ds = await asyncio.gather(*ds_tasks)
    except Exception as exc:  # noqa: BLE001 — corrupt/undecodable sheet or variant
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Could not decode an input image"
        ) from exc
    orig_fit = ds[0][0]
    result_fit = ds[1][0]
    new_fit = ds[2][0]
    old_fit: Optional[bytes] = ds[3][0] if (has_old and old_sheet is not None) else None

    # 4. Build prompt vars (byte-independent guide/manifests) → render prompt + model.
    mix_inputs = _build_mix_crop_inputs(req.crops)
    crop_spec, new_spec, old_spec = _build_specs(orig_fit, old_fit, new_fit)
    built = build_mix_references(
        crop_spec, new_spec, old_spec, req.swap_targets, layout.cells, mix_inputs
    )
    variables = {
        "image_guide": build_mix_detect_image_guide(n_crops, n_targets, has_old, True),
        "variant_manifest": built["manifest_vars"]["variant_manifest"],
        "crop_manifest": built["manifest_vars"]["crop_manifest"],
        "builder_params": build_detect_builder_params(req.swap_model, req.swap_temperature),
        "cell_count": str(n_crops),
        "target_count": str(n_targets),
    }
    try:
        rendered_prompt, model = await load_and_render(
            DETECT_MIX_DEFECTS_SYSTEM_NAME,
            variables,
            default_model=DETECT_MIX_DEFECTS_DEFAULT_MODEL,
        )
    except PromptTemplateNotFound as exc:
        raise RemixDomainError(
            status=500, code="PROMPT_TEMPLATE_NOT_FOUND",
            message="detect-mix-defects prompt missing — seed not applied?",
        ) from exc

    # 5. Hard-guard inline payload (keep RESULT sharp) → rebuild parts on final
    #    bytes. content_parts contract order: [orig, old?, new, result].
    orig_fit, old_fit, new_fit, result_fit = await _fit_mix_detect_payload(
        orig_fit, old_fit, new_fit, result_fit, rendered_prompt
    )
    crop_spec, new_spec, old_spec = _build_specs(orig_fit, old_fit, new_fit)
    built = build_mix_references(
        crop_spec, new_spec, old_spec, req.swap_targets, layout.cells, mix_inputs
    )
    image_parts = [*built["parts"], _img_part(result_fit, "image/png")]

    # 6. Gemini call (1×, structured JSON defects).
    gemini_t0 = time.monotonic()
    defects_raw, token = await _run_detect_gemini(
        rendered_prompt, image_parts, model, ai_context=ai_context
    )
    gemini_ms = int((time.monotonic() - gemini_t0) * 1000)

    # 7. Map → circles + filter + sort + cap (shared engine, MIX categories).
    defects, raw_count, truncated = map_defects_to_circles(
        defects_raw, w_s, h_s,
        focus_objects=req.focus_objects,
        severity_threshold=req.severity_threshold,
        max_defects=req.max_defects,
    )

    meta = DetectMixDefectsMeta(
        cellCount=n_crops,
        targetCount=n_targets,
        defectCount=len(defects),
        rawDefectCount=raw_count,
        truncated=truncated or None,
        swappedDimensions=SwappedDimensions(width=w_s, height=h_s),
        hasOldVariantSheet=has_old,
        processingTimeMs=int((time.monotonic() - t0) * 1000),
        tokenUsage=token,
    )
    logger.info(
        "detect_mix_defects_done W_s=%d H_s=%d cells=%d targets=%d has_old=%s raw=%d "
        "defects=%d truncated=%s gemini_ms=%d",
        w_s, h_s, n_crops, n_targets, has_old, raw_count, len(defects), truncated, gemini_ms,
    )
    return DetectMixDefectsResult(defects=defects, meta=meta)
