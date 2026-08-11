"""detect-swap-defects core (`run_detect_swap_defects`) — swap defect localization.

Spec: ai-storybook-design/api/remix/06-detect-swap-defects.md.

Image-IN / defect-regions-OUT. ONE Gemini multimodal call compares the swap RESULT
against 3 reference groups — the ORIGINAL sprite sheet (#1), M HUMAN refs (#2..#M+1),
and the RESULT itself (#M+2) — plus the per-cell swap_plan + builder_params text, and
returns a free list of defect boxes (`[ymin,xmin,ymax,xmax]` 0-1000 on the RESULT).
The server converts each box → px circle (center + half-diagonal radius) + keeps the
px box, then filters (focus/severity) → sorts (severity desc, confidence desc) → caps.

RESULT (commit ba0ae4a): RECOMPOSED in-process from `result_crops[]` via the SAME
`compose_crop_sheet` as the ORIGINAL → both sheets share `sheet_geometry`
(`swappedDimensions == sheet_geometry`, byte-aligned gutter/grid/ordinals). Every
input image is cost-downscaled (`downscale_for_ai_cost`) before Gemini; the 0-1000→px
basis is `sheet_geometry` (measured BEFORE downscale), so the downscale is lossless to
the coordinates.

Advisory / non-fatal: every failure raises `RemixDomainError` (router → spec envelope);
the caller treats ANY error as "couldn't inspect" and keeps the swap result. An empty
`defects` list is SUCCESS (ran + found nothing wrong) — distinct from an error.

Entry: the HTTP route is the primary caller (FE "check" button per batch/sheet in the
Remix batch sidebar). The core also accepts optional pre-fetched bytes for testability +
a future in-process job caller — NOT wired in this plan.

PII discipline (parity 03): NEVER log/echo raw URLs, image bytes, base64,
`human_description`, `swap_traits[].description`, or `defect.message`. DEBUG logs carry
counts + box coordinates only.
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
from src.models.requests.detect_swap_defects import (
    DETECT_SWAP_DEFECTS_DEFAULT_MODEL,
    DETECT_SWAP_DEFECTS_SYSTEM_NAME,
    DETECT_TEMPERATURE,
    DETECT_TIMEOUT_S,
    MAX_DEFECT_MESSAGE_LEN,
    MAX_DETECT_RETRIES,
    MAX_IMAGE_BYTES,
    MAX_SWAPPED_SHEET_BYTES,
    SWAP_DEFECT_CATEGORIES,
    DefectBox,
    DefectPoint,
    DetectSwapDefectsMeta,
    DetectSwapDefectsRequest,
    SwapDefect,
    SwappedDimensions,
)
from src.models.requests.swap_sprite_sheet import sprite_crops_as_base
from src.services import http_fetch
from src.services.ai_image_downscale import (
    HUMAN_AI_MAX_EDGE,
    SHEET_AI_MAX_EDGE,
    downscale_for_ai_cost,
)
from src.services.gemini.payload_budget import (
    GEMINI_INLINE_LIMIT_BYTES,
    BudgetExceededError,
    compute_base64_size,
    fit_group_to_budget,
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
from src.services.remix.swap_sprite_prompt_builder import (
    SpriteObjectInput,
    build_sprite_references,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFECTS_SCHEMA",
    "DetectSwapDefectsResult",
    "build_detect_builder_params",
    "map_defects_to_circles",
    "run_detect_swap_defects",
]

_RAW_LOG_CAP = 160
_SAFETY_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}

# Separate concurrency pool from the image-preview swap (`_gemini_sem`): detect is a
# cheaper TEXT/vision model (gemini-3.5-flash) on a different rate-limit pool, so it
# must not compete for the image-gen slots. Bounds the +1 flash call/sheet cost.
_DETECT_CONCURRENCY_CAP = 3
_DETECT_SEM = asyncio.Semaphore(_DETECT_CONCURRENCY_CAP)

_REINFORCE = (
    "\n\nNHẮC LẠI: chỉ trả JSON đúng schema "
    '{ "defects": [ { "box": [ymin,xmin,ymax,xmax], "category": "<optional>", '
    '"severity": "<optional>", "cell": <optional>, "object_key": "<optional>", '
    '"message": "<optional>", "confidence": <optional> } ] }. '
    "box hệ 0-1000 trên ẢNH KẾT QUẢ (ảnh cuối). KHÔNG thấy lỗi → defects:[]. KHÔNG văn xuôi."
)

# Gemini structured-output schema — defects only. `box` required; everything else
# optional (the model may omit annotations). Server validates + drops bad items.
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
class DetectSwapDefectsResult:
    """Core output — the router builds the response envelope from this."""

    defects: list[SwapDefect]
    meta: DetectSwapDefectsMeta


# ─── builder_params text (detect-only prompt var) ────────────────────────────


def build_detect_builder_params(
    model: Optional[str], temperature: Optional[float]
) -> str:
    """`{%request.builder_params%}` — the swap params used + the invariants Gemini
    must treat as "must hold" (a violation at a region = a defect there). Generic
    text only (model/temp + invariants); NO human data embedded."""
    model_str = model or "(mặc định)"
    temp_str = str(temperature) if temperature is not None else "(mặc định)"
    return (
        f"- Model image-gen đã dùng: {model_str} ; temperature: {temp_str}.\n"
        "- Swap chạy 1 call image-gen DUY NHẤT cho cả sheet.\n"
        "- BẮT BUỘC giữ (vi phạm = vùng lỗi): lưới + số ô (ordinal), pose + biểu "
        "cảm từng ô, art style, và MỌI trait KHÔNG nằm trong swap[] của ô đó. Chỉ "
        "thay đúng trait được liệt kê, bằng đúng diện mạo human ref của object ô đó."
    )


# ─── Gemini parse helpers ────────────────────────────────────────────────────


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
    dropped at map-time, not here (here we only guarantee a list of dicts).
    Globally malformed (non-JSON / no array) → `_ParseRetry` on the first attempt,
    `RemixDomainError(500, PARSE_ERROR)` on the final attempt.
    """
    cleaned = _strip_fence(raw_text)

    def _bad(msg: str):
        logger.warning("detect_defects_parse_invalid final=%s raw=%s", final, cleaned[:_RAW_LOG_CAP])
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
    safety exc to `RemixDomainError` (502 LLM_ERROR / 504→502 / 422 safety)."""
    try:
        async with _DETECT_SEM:
            result = await gemini_ainvoke(
                model=model,
                messages=[HumanMessage(content=content_parts)],
                run_name="detect-swap-defects",
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
        logger.warning("detect_defects_gemini_error code=%s err=%s", code, str(exc)[:_RAW_LOG_CAP])
        raise RemixDomainError(status=502, code="LLM_ERROR", message="Gemini request failed") from exc


@traceable(name="remix.detect_swap_defects.gemini", run_type="llm")
async def _run_detect_gemini(
    rendered_prompt: str,
    image_parts: list[dict],
    model: str,
    *,
    ai_context: AiCallContext | None = None,
) -> tuple[list[dict], Optional[int]]:
    """Gemini defect localization. `image_parts` = [orig, *humans, result] in that
    contract order. 1 app-level parse-retry with reinforcement; langchain
    `max_retries` covers transient transport. Raises `RemixDomainError` on
    safety/LLM/parse failure. `ai_context` (Phase 05) attributes each Gemini call."""
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


# ─── Pure mapping: box → circle + filter + sort + cap (DRY 06↔07) ────────────


def _make_swap_defect(
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
    sprite-plane `SwapDefect` from the engine's computed scalar fields."""
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
    """Convert raw Gemini defects → `SwapDefect[]` on the RESULT image.

    Thin wrapper over the shared `defect_postprocess.map_defects_to_circles`
    engine (DRY 06↔07): injects the sprite-plane `SwapDefect` factory + the
    10-category set + the message cap. Pure + deterministic; returns
    `(defects, raw_count, truncated)`.
    """
    return _map_defects_engine(
        raw_defects,
        w_s,
        h_s,
        defect_factory=_make_swap_defect,
        categories=SWAP_DEFECT_CATEGORIES,
        focus_objects=focus_objects,
        severity_threshold=severity_threshold,
        max_defects=max_defects,
        max_message_len=MAX_DEFECT_MESSAGE_LEN,
    )


# ─── Image prep ──────────────────────────────────────────────────────────────


def _http_exc_code(exc: HTTPException) -> Optional[str]:
    detail = exc.detail
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, str):
                return code
    return None


async def _identity_bytes(data: bytes, mime: str) -> tuple[bytes, str]:
    """Wrap pre-fetched bytes as an awaitable so an in-process caller's bytes can
    join the same `asyncio.gather` as the fetched images (no special-casing)."""
    return data, mime


async def _fetch_image(url: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[bytes, str]:
    """SSRF-guarded fetch → (bytes, mime). SSRF block → 400 SSRF_BLOCKED (spec
    envelope); any other failure → 422 IMAGE_FETCH_ERROR. No URL logged.

    `max_bytes` defaults to the 10MB single-image cap; sheet-class assets (swapped
    result / original sheet) pass the larger `MAX_SWAPPED_SHEET_BYTES`."""
    try:
        return await http_fetch.fetch_image_bytes(url, max_bytes=max_bytes, timeout_s=30.0)
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
    req: DetectSwapDefectsRequest, original_bytes: Optional[bytes], original_mime: Optional[str]
) -> tuple[bytes, str]:
    """ORIGINAL sheet: pre-fetched → use; `original_sheet_url` → fetch; else compose
    from `crops[]` (ALL_CROPS_FAILED bubbles up from the composer)."""
    if original_bytes is not None:
        return original_bytes, original_mime or "image/png"
    if req.original_sheet_url:
        return await _fetch_image(req.original_sheet_url, max_bytes=MAX_SWAPPED_SHEET_BYTES)
    composed = await compose_crop_sheet(
        BuildCropSheetRequest(
            sheet_geometry=req.sheet_geometry,
            crops=sprite_crops_as_base(req.crops),
            frame=FrameStyle(),  # composer defaults: ordinal badge ON
            response_format="base64",
        )
    )
    return composed.png_bytes, "image/png"


async def _prepare_result(
    req: DetectSwapDefectsRequest,
    result_bytes: Optional[bytes],
    result_mime: Optional[str],
) -> tuple[bytes, str]:
    """RESULT sheet: pre-composed bytes (test / in-process caller) → use; else
    RECOMPOSE from `result_crops[]` via the SAME composer as the ORIGINAL (same
    `FrameStyle` defaults → ordinal badge ON → pixel-aligned with the ORIGINAL).

    The composer raises the GENERIC `ALL_CROPS_FAILED` when every result piece fails
    to fetch/decode; we re-map it to `ALL_RESULT_CROPS_FAILED` so the caller can
    tell a RESULT-side total failure apart from an ORIGINAL-side one (which keeps
    `ALL_CROPS_FAILED`). A PARTIAL failure is accepted as advisory (validation S1):
    the composer fills the failed cells with gutter, and a missing swapped piece
    reads as a true defect at that cell."""
    if result_bytes is not None:
        return result_bytes, result_mime or "image/png"
    try:
        composed = await compose_crop_sheet(
            BuildCropSheetRequest(
                sheet_geometry=req.sheet_geometry,
                crops=sprite_crops_as_base(req.result_crops),
                frame=FrameStyle(),  # SAME defaults as ORIGINAL: ordinal badge ON
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
    return composed.png_bytes, "image/png"


async def _fit_detect_payload(
    orig: bytes, humans: list[bytes], result: bytes, prompt: str
) -> tuple[bytes, list[bytes], bytes]:
    """Hard-guard the inline payload while keeping the RESULT image sharpest.

    Priority (validation S1 Q4): RESULT untouched (the inspection target) → shrink
    HUMANS first (pool, already pre-shrunk) → then ORIGINAL → only as a last resort
    shrink the RESULT. Uses public budget helpers (`fit_group_to_budget` /
    `fit_to_budget`) on derived RAW budgets (b64 ≈ 4/3 raw). Returns the (possibly
    shrunk) `(orig, humans, result)`.
    """
    cap = GEMINI_INLINE_LIMIT_BYTES
    prompt_b64 = len(prompt.encode("utf-8"))
    margin = 1024

    def total() -> int:
        return (
            compute_base64_size(len(orig))
            + sum(compute_base64_size(len(h)) for h in humans)
            + compute_base64_size(len(result))
            + prompt_b64
        )

    if total() + margin <= cap:
        return orig, humans, result

    def _raw_budget(reserved_b64: int) -> int:
        avail_b64 = cap - reserved_b64 - margin
        return max(256 * 1024, avail_b64 * 3 // 4)

    # Tier 1 — shrink humans (keep orig + result).
    if humans:
        reserved = compute_base64_size(len(orig)) + compute_base64_size(len(result)) + prompt_b64
        humans = await fit_group_to_budget(humans, _raw_budget(reserved))
        if total() + margin <= cap:
            return orig, humans, result

    # Tier 2 — shrink original (keep result).
    reserved = (
        sum(compute_base64_size(len(h)) for h in humans)
        + compute_base64_size(len(result))
        + prompt_b64
    )
    try:
        orig = await fit_to_budget(orig, _raw_budget(reserved))
    except BudgetExceededError:
        pass
    if total() + margin <= cap:
        return orig, humans, result

    # Tier 3 (pathological) — last resort, shrink the result too.
    reserved = (
        compute_base64_size(len(orig))
        + sum(compute_base64_size(len(h)) for h in humans)
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
    return orig, humans, result


def _build_object_inputs(req: DetectSwapDefectsRequest) -> list[SpriteObjectInput]:
    return [
        SpriteObjectInput(
            object_key=o.object_key,
            name=o.object_context.name,
            swap_traits=[(t.type, t.description) for t in o.swap_traits],
        )
        for o in req.swap_objects
    ]


def _build_specs(
    orig: bytes, humans: list[bytes], result: bytes
) -> tuple[ReferenceSpec, list[ReferenceSpec], ReferenceSpec]:
    # All three groups are PNG after `downscale_for_ai_cost(reencode='png')` — keeps
    # flat regions (gutter / cell strokes / ordinal badges) crisp for pixel-aligned
    # comparison + preserves alpha. (The rare hard-guard tier may JPEG-shrink them,
    # but Gemini sniffs the actual bytes — the declared mime stays advisory.)
    orig_spec = ReferenceSpec(
        role=ReferenceRole.CROP_SHEET, image_bytes=orig, mime_type="image/png"
    )
    human_specs = [
        ReferenceSpec(role=ReferenceRole.HUMAN_REF, image_bytes=h, mime_type="image/png")
        for h in humans
    ]
    # The RESULT role is irrelevant to the part shape (the guide line is appended by
    # the builder's `has_result` branch, not ROLE_USAGE).
    result_spec = ReferenceSpec(
        role=ReferenceRole.CROP_SHEET, image_bytes=result, mime_type="image/png"
    )
    return orig_spec, human_specs, result_spec


# ─── Core ────────────────────────────────────────────────────────────────────


@traceable(name="remix_detect_swap_defects")
async def run_detect_swap_defects(
    req: DetectSwapDefectsRequest,
    *,
    result_bytes: Optional[bytes] = None,
    result_mime: Optional[str] = None,
    original_bytes: Optional[bytes] = None,
    original_mime: Optional[str] = None,
    human_bytes: Optional[list[bytes]] = None,
    ai_context: AiCallContext | None = None,
) -> DetectSwapDefectsResult:
    """detect-swap-defects core. Optional pre-fetched/pre-composed bytes let a test
    or in-process caller skip the recompose/re-fetch (NOT wired in this plan —
    caller = FE direct HTTP). `result_bytes` short-circuits the `result_crops[]`
    recompose."""
    t0 = time.monotonic()
    n_crops = len(req.crops)
    n_objects = len(req.swap_objects)

    # 1. Prepare images IN PARALLEL, all SSRF-guarded:
    #      RESULT   = recompose from `result_crops[]` (or pre-composed bytes),
    #      ORIGINAL = `original_sheet_url` fast-path OR compose from `crops[]`,
    #      HUMANS   = M refs.
    #    When the ORIGINAL is COMPOSED (no fast-path — job 11's path), it shares the
    #    SAME composer + `sheet_geometry` as the RESULT → the two sheets are
    #    pixel-aligned. The `original_sheet_url` fast-path is fetched at arbitrary
    #    dims (a visual reference only, NOT dim-aligned). Either way the 0-1000→px
    #    basis is `sheet_geometry` (taken here, BEFORE the cost-downscale below), so
    #    the defect coordinates are always correct.
    result_task = _prepare_result(req, result_bytes, result_mime)
    orig_task = _prepare_original(req, original_bytes, original_mime)
    if human_bytes is None:
        human_tasks = [_fetch_image(o.human_image_url) for o in req.swap_objects]
    else:
        human_tasks = [_identity_bytes(b, "image/png") for b in human_bytes]
    gathered = await asyncio.gather(result_task, orig_task, *human_tasks)
    result_raw, _result_mime = gathered[0]
    orig_raw, _orig_mime = gathered[1]
    raw_humans = [b for (b, _m) in gathered[2:]]

    # Coordinate basis = sheet_geometry. `compose_crop_sheet` guarantees the composed
    # RESULT is EXACTLY `sheet_geometry`, so no measure step is needed (commit ba0ae4a).
    w_s = req.sheet_geometry.width
    h_s = req.sheet_geometry.height

    # 2. Cost-downscale every input for Gemini (CPU-bound → to_thread). BOTH sheets
    #    use the SAME `SHEET_AI_MAX_EDGE` → SAME out dims → stay pixel-aligned; humans
    #    use `HUMAN_AI_MAX_EDGE`. `reencode='png'` keeps gutter/grid/ordinals crisp.
    #    Coordinates are UNAFFECTED (the 0-1000→px convert uses `sheet_geometry`).
    try:
        downscaled = await asyncio.gather(
            asyncio.to_thread(
                downscale_for_ai_cost, orig_raw, max_edge=SHEET_AI_MAX_EDGE, reencode="png"
            ),
            asyncio.to_thread(
                downscale_for_ai_cost, result_raw, max_edge=SHEET_AI_MAX_EDGE, reencode="png"
            ),
            *[
                asyncio.to_thread(
                    downscale_for_ai_cost, h, max_edge=HUMAN_AI_MAX_EDGE, reencode="png"
                )
                for h in raw_humans
            ],
        )
    except Exception as exc:  # noqa: BLE001 — corrupt/undecodable sheet or ref
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Could not decode an input image"
        ) from exc
    orig_fit = downscaled[0][0]
    result_fit = downscaled[1][0]
    humans_fit = [d[0] for d in downscaled[2:]]

    object_inputs = _build_object_inputs(req)

    # 3. Build prompt vars (byte-independent guide/plan) → render prompt + model.
    orig_spec, human_specs, result_spec = _build_specs(orig_fit, humans_fit, result_fit)
    built = build_sprite_references(
        orig_spec, human_specs, object_inputs, req.crops,
        result_spec=result_spec, has_result=True,
    )
    variables = {
        "image_guide": built["guide_text"],
        "swap_plan": built["manifest_vars"]["cell_swap_plan"],
        "builder_params": build_detect_builder_params(req.swap_model, req.swap_temperature),
        "cell_count": str(n_crops),
        "object_count": str(n_objects),
    }
    try:
        rendered_prompt, model = await load_and_render(
            DETECT_SWAP_DEFECTS_SYSTEM_NAME,
            variables,
            default_model=DETECT_SWAP_DEFECTS_DEFAULT_MODEL,
        )
    except PromptTemplateNotFound as exc:
        raise RemixDomainError(
            status=500, code="PROMPT_TEMPLATE_NOT_FOUND",
            message="detect-swap-defects prompt missing — seed not applied?",
        ) from exc

    # 4. Hard-guard inline payload (keep result sharp) → rebuild parts on final bytes.
    orig_fit, humans_fit, result_fit = await _fit_detect_payload(
        orig_fit, humans_fit, result_fit, rendered_prompt
    )
    orig_spec, human_specs, result_spec = _build_specs(orig_fit, humans_fit, result_fit)
    built = build_sprite_references(
        orig_spec, human_specs, object_inputs, req.crops,
        result_spec=result_spec, has_result=True,
    )

    # 5. Gemini call (1×, structured JSON defects).
    gemini_t0 = time.monotonic()
    defects_raw, token = await _run_detect_gemini(
        rendered_prompt, built["parts"], model, ai_context=ai_context
    )
    gemini_ms = int((time.monotonic() - gemini_t0) * 1000)

    # 6. Map → circles + filter + sort + cap.
    defects, raw_count, truncated = map_defects_to_circles(
        defects_raw, w_s, h_s,
        focus_objects=req.focus_objects,
        severity_threshold=req.severity_threshold,
        max_defects=req.max_defects,
    )

    meta = DetectSwapDefectsMeta(
        cellCount=n_crops,
        objectCount=n_objects,
        defectCount=len(defects),
        rawDefectCount=raw_count,
        truncated=truncated or None,
        swappedDimensions=SwappedDimensions(width=w_s, height=h_s),
        processingTimeMs=int((time.monotonic() - t0) * 1000),
        tokenUsage=token,
    )
    logger.info(
        "detect_swap_defects_done W_s=%d H_s=%d cells=%d objects=%d raw=%d defects=%d "
        "truncated=%s gemini_ms=%d",
        w_s, h_s, n_crops, n_objects, raw_count, len(defects), truncated, gemini_ms,
    )
    return DetectSwapDefectsResult(defects=defects, meta=meta)
