"""detect-rmbg-defects core (`run_detect_rmbg_defects`) — remove-bg defect localization.

Spec: ai-storybook-design/api/remix/08-detect-rmbg-defects.md (AUTHORITATIVE).
3rd plane of the detect family (after sprite 06 + mix 07). THE SIMPLEST.

Image-IN / defect-regions-OUT. ONE Gemini multimodal call compares the RESULT
cut-out sheet (#2 — RGBA TRANSPARENT, the inspection target) against the ORIGINAL
still-background sheet (#1 — opaque, the SUBJECT-vs-BACKGROUND reference) and
returns a free list of defect boxes (`[ymin,xmin,ymax,xmax]` 0-1000 on the
RESULT, the LAST image). The server converts each box → px circle (center +
half-diagonal radius) + keeps the px box, then filters (severity) → sorts
(severity desc, confidence desc) → caps — all via the SHARED `defect_postprocess`
engine (DRY 06↔07↔08). `cell` is then assigned SERVER-SIDE by hit-testing each
defect center against `crops[].geometry` (the sheet is composed PLAIN — no badge
for Gemini to read).

⚡ ALPHA PIPELINE (spec Risk #1): the RESULT image is RGBA-transparent
END-TO-END — `compose_crop_sheet(..., transparent_canvas=True)` keeps the cut-out
alpha → `downscale_for_ai_cost(..., reencode='png')` keeps alpha (PNG, no
convert-to-RGB) → Gemini reads the transparency directly (transparent = removed,
opaque = kept). NEVER flatten / convert('RGB') / JPEG the RESULT — that destroys
the remove-bg signal. The ORIGINAL stays opaque PNG.

Why a SEPARATE core from 06/07? rmbg has NO identity / human-ref / variant-sheet
/ swap_plan / swap_objects — just 2 images, an RGBA-transparent result, an
rmbg-specific category set, and a "MASK ONLY, not content" prompt. Shared infra:
`crop_sheet_composer` (×2 ORIGINAL opaque + RESULT transparent), `ai_image_downscale`
(alpha-preserving path), `gemini_payload_budget`, `defect_postprocess`, and the 06
response leaves (`DefectPoint`/`DefectBox`/`SwappedDimensions`).

RESULT (parity 06/07): RECOMPOSED in-process from `result_crops[]` via the SAME
`compose_crop_sheet` as the ORIGINAL → both sheets share `sheet_geometry`
(`swappedDimensions == sheet_geometry`, measured BEFORE the cost-downscale, so the
0-1000→px basis is unaffected by it).

Advisory / non-fatal: every failure raises `RemixDomainError` (router → spec
envelope); the caller treats ANY error as "couldn't inspect" and keeps the rmbg
result. An empty `defects` list is SUCCESS (ran + found nothing wrong).

PII discipline (parity 06/07): NEVER log/echo raw URLs, image bytes, base64, or
`defect.message`. DEBUG logs carry counts + box counts only.
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

from src.models.requests.build_crop_sheet import (
    BuildCropSheetRequest,
    Crop,
    FrameStyle,
)
from src.models.requests.detect_rmbg_defects import (
    DETECT_RMBG_DEFECTS_DEFAULT_MODEL,
    DETECT_RMBG_DEFECTS_SYSTEM_NAME,
    DETECT_TEMPERATURE,
    DETECT_TIMEOUT_S,
    MAX_DEFECT_MESSAGE_LEN,
    MAX_DETECT_RETRIES,
    MAX_IMAGE_BYTES,
    MAX_SHEET_FETCH_BYTES,
    RMBG_DEFECT_CATEGORIES,
    DefectBox,
    DefectPoint,
    DetectRmbgDefectsMeta,
    DetectRmbgDefectsRequest,
    RmbgCrop,
    RmbgDefect,
    SwappedDimensions,
)
from src.services import http_fetch
from src.services.ai_image_downscale import (
    SHEET_AI_MAX_EDGE,
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
from src.services.remix.crop_sheet_composer import compose_crop_sheet
from src.services.remix.defect_postprocess import (
    map_defects_to_circles as _map_defects_engine,
)
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)

__all__ = [
    "DEFECTS_SCHEMA",
    "DetectRmbgDefectsResult",
    "build_rmbg_detect_image_guide",
    "map_defects_to_circles",
    "run_detect_rmbg_defects",
]

_SAFETY_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}

# PLAIN frame for BOTH sheets (parity job 09): no cell stroke, no ordinal badge —
# a baked badge/stroke would survive into the RGBA result + give Gemini a number
# to read (we assign `cell` server-side instead). `draw_ordinals=False` MUST be
# explicit (not None) so the composer's `is not None` resolve keeps it False.
_PLAIN_FRAME = FrameStyle(cell_stroke_width=0, draw_ordinals=False)

# Separate concurrency pool from the image-preview swap: detect is a cheaper
# TEXT/vision model (gemini-3.5-flash) on a different rate-limit pool, so it must
# not compete for the image-gen slots. Bounds the +1 flash call/sheet.
_DETECT_CONCURRENCY_CAP = 3
_DETECT_SEM = asyncio.Semaphore(_DETECT_CONCURRENCY_CAP)

_REINFORCE = (
    "\n\nNHẮC LẠI: chỉ trả JSON đúng schema "
    '{ "defects": [ { "box": [ymin,xmin,ymax,xmax], "category": "<optional>", '
    '"severity": "<optional>", "cell": <optional>, "message": "<optional>", '
    '"confidence": <optional> } ] }. '
    "box hệ 0-1000 trên ẢNH KẾT QUẢ (ảnh CUỐI — đã tách nền, RGBA trong suốt). "
    "CHỈ soi mask tách nền, KHÔNG đánh giá nội dung tranh. KHÔNG thấy lỗi → "
    "defects:[]. KHÔNG văn xuôi."
)

# Gemini structured-output schema — defects only. `box` required; everything else
# optional. NO `object_key` (rmbg has no swap target). Server validates + drops bad
# items.
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
class DetectRmbgDefectsResult:
    """Core output — the router builds the response envelope from this."""

    defects: list[RmbgDefect]
    meta: DetectRmbgDefectsMeta


# ─── Image guide (deterministic prompt var) ──────────────────────────────────


def build_rmbg_detect_image_guide() -> str:
    """`{%request.image_guide%}` — role of the 2 FIXED images + the transparency
    convention + the "MASK ONLY, not content" scope. NO color/backing param."""
    return "\n".join(
        [
            "- Ảnh #1 (GỐC, CÒN NỀN): crop sheet TRƯỚC khi tách nền — lưới ô, mỗi ô là "
            "mảnh tranh còn nguyên nền (đặc). Dùng để biết đâu là CHỦ THỂ (phải giữ) và "
            "đâu là NỀN (phải bỏ). KHÔNG soi lỗi trên ảnh này.",
            "- Ảnh #2 (KẾT QUẢ, ĐÃ TÁCH NỀN): CÙNG lưới/ô, đã remove-bg — ảnh PNG có "
            "kênh trong suốt. Quy ước: vùng TRONG SUỐT = đã bỏ (nền); vùng ĐẶC = giữ lại "
            "(chủ thể). ĐÂY là ảnh soi lỗi — mọi toạ độ box tính trên ảnh NÀY.",
            "- Đối chiếu từng vùng: vùng ở GỐC là NỀN nhưng KẾT QUẢ còn đục → thừa nền; "
            "vùng ở GỐC là CHỦ THỂ nhưng KẾT QUẢ thành trong suốt → thiếu (lỗ thủng). "
            "CHỈ soi mask tách nền; KHÔNG đánh giá nét vẽ/màu/nội dung.",
        ]
    )


# ─── Pure mapping: box → circle + filter + sort + cap (DRY 06↔07↔08) ─────────


def _make_rmbg_defect(
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
    object_key: Optional[str] = None,  # accepted for engine parity; IGNORED (no target)
) -> RmbgDefect:
    """Factory passed to the shared `defect_postprocess` engine — builds ONE
    RMBG-plane `RmbgDefect` (category ∈ RmbgDefectCategory, NO object_key). The
    engine-supplied `cell` is a placeholder; the core OVERRIDES it with a
    geometry hit-test after mapping."""
    return RmbgDefect(
        center=DefectPoint(x=center_x, y=center_y),
        radius=radius,
        box=DefectBox(x=box_x, y=box_y, w=box_w, h=box_h),
        category=category,
        severity=severity,
        message=message,
        confidence=confidence,
        cell=cell,
    )


def map_defects_to_circles(
    raw_defects: list[dict],
    w_s: int,
    h_s: int,
    *,
    severity_threshold: Optional[str] = None,
    max_defects: int = 30,
) -> tuple[list[RmbgDefect], int, bool]:
    """Convert raw Gemini defects → RMBG `RmbgDefect[]` on the RESULT image.

    Thin wrapper over the shared `defect_postprocess` engine: injects the
    rmbg-plane `RmbgDefect` factory + the 7 RMBG categories + the message cap. No
    `focus_objects` (rmbg has no target identity).
    """
    return _map_defects_engine(
        raw_defects,
        w_s,
        h_s,
        defect_factory=_make_rmbg_defect,
        categories=RMBG_DEFECT_CATEGORIES,
        severity_threshold=severity_threshold,
        max_defects=max_defects,
        max_message_len=MAX_DEFECT_MESSAGE_LEN,
    )


def _assign_cells(defects: list[RmbgDefect], crops: list[RmbgCrop]) -> None:
    """Assign `defect.cell` SERVER-SIDE: ordinal (index+1) of the FIRST crop whose
    geometry contains the defect center. Best-effort — a center in the gutter (no
    cell) → `cell = None`. Overrides whatever the engine/Gemini put on `.cell`."""
    for d in defects:
        cx, cy = d.center.x, d.center.y
        assigned: Optional[int] = None
        for idx, c in enumerate(crops):
            g = c.geometry
            if g.x <= cx < g.x + g.w and g.y <= cy < g.y + g.h:
                assigned = idx + 1
                break
        d.cell = assigned


# ─── Gemini parse helpers (per-core; invoke goes through the shared
#     `gemini_ainvoke` seam — src/services/gemini/invoke.py) ─────────────────


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
        logger.warning(
            "detect_rmbg_defects_parse_invalid final=%s cleaned_len=%d", final, len(cleaned)
        )
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
                run_name="detect-rmbg-defects",
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
        logger.warning("detect_rmbg_defects_gemini_error code=%s exc_type=%s", code, type(exc).__name__)
        raise RemixDomainError(status=502, code="LLM_ERROR", message="Gemini request failed") from exc


async def _run_detect_gemini(
    rendered_prompt: str,
    image_parts: list[dict],
    model: str,
    *,
    ai_context: AiCallContext | None = None,
) -> tuple[list[dict], Optional[int]]:
    """Gemini defect localization. `image_parts` = [orig (opaque), result (RGBA
    transparent)] — result is the LAST/inspection image (contract). 1 app-level
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


def _rmbg_crops_as_base(crops: list[RmbgCrop]) -> list[Crop]:
    """Adapt lean `RmbgCrop` → the composer's base `Crop` (id/media_url/geometry).
    No annotation/objects (rmbg has no manifest)."""
    return [Crop(id=c.id, media_url=c.media_url, geometry=c.geometry) for c in crops]


async def _prepare_original(
    req: DetectRmbgDefectsRequest, original_bytes: Optional[bytes]
) -> bytes:
    """ORIGINAL still-bg sheet (OPAQUE): pre-fetched → use; `original_sheet_url` →
    fetch (large DoS bound); else compose PLAIN from `crops[]` (ALL_CROPS_FAILED
    bubbles from the composer)."""
    if original_bytes is not None:
        return original_bytes
    if req.original_sheet_url:
        return await _fetch_image(req.original_sheet_url, max_bytes=MAX_SHEET_FETCH_BYTES)
    composed = await compose_crop_sheet(
        BuildCropSheetRequest(
            sheet_geometry=req.sheet_geometry,
            crops=_rmbg_crops_as_base(req.crops),
            frame=_PLAIN_FRAME,  # plain — no badge/stroke (parity job 09)
            response_format="base64",
        )
    )
    return composed.png_bytes


async def _prepare_result(
    req: DetectRmbgDefectsRequest, result_bytes: Optional[bytes]
) -> bytes:
    """RESULT cut-out sheet (RGBA TRANSPARENT): pre-composed bytes → use;
    `result_sheet_url` → fetch the persisted RGBA sheet (alpha already baked);
    else RECOMPOSE from `result_crops[]` via the SAME composer as the ORIGINAL but
    with `transparent_canvas=True` so the cut-out alpha is preserved
    (pixel-aligned with the ORIGINAL). The composer's GENERIC `ALL_CROPS_FAILED`
    (every result piece failed) is re-mapped to `ALL_RESULT_CROPS_FAILED`."""
    if result_bytes is not None:
        return result_bytes
    if req.result_sheet_url:
        return await _fetch_image(req.result_sheet_url, max_bytes=MAX_SHEET_FETCH_BYTES)
    try:
        composed = await compose_crop_sheet(
            BuildCropSheetRequest(
                sheet_geometry=req.sheet_geometry,
                crops=_rmbg_crops_as_base(req.result_crops),
                frame=_PLAIN_FRAME,  # SAME plain frame as ORIGINAL → pixel-aligned
                response_format="base64",
            ),
            transparent_canvas=True,  # ⚡ keep cut-out alpha (no flatten)
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


async def _fit_rmbg_detect_payload(
    orig: bytes, result: bytes, prompt: str
) -> tuple[bytes, bytes]:
    """Hard-guard the inline payload. With 2 PNG sheets this NEVER binds — pure
    safety net. If it does, shrink ONLY the ORIGINAL (opaque): the RESULT alpha is
    the remove-bg signal and `fit_to_budget` JPEG-strips alpha, so we refuse to
    touch the RESULT and raise INTERNAL instead of destroying the signal."""
    cap = GEMINI_INLINE_LIMIT_BYTES
    prompt_b64 = len(prompt.encode("utf-8"))
    margin = 1024

    def total() -> int:
        return compute_base64_size(len(orig)) + compute_base64_size(len(result)) + prompt_b64

    if total() + margin <= cap:
        return orig, result

    reserved = compute_base64_size(len(result)) + prompt_b64
    avail_b64 = cap - reserved - margin
    raw_budget = max(256 * 1024, avail_b64 * 3 // 4)
    try:
        orig = await fit_to_budget(orig, raw_budget)
    except BudgetExceededError:
        pass
    if total() + margin > cap:
        raise RemixDomainError(
            status=500, code="INTERNAL",
            message="Detect payload exceeds Gemini inline budget after hard-guard "
            "(RESULT alpha must not be flattened)",
        )
    return orig, result


# ─── Core ────────────────────────────────────────────────────────────────────


@traceable(name="remix_detect_rmbg_defects")
async def run_detect_rmbg_defects(
    req: DetectRmbgDefectsRequest,
    *,
    result_bytes: Optional[bytes] = None,
    original_bytes: Optional[bytes] = None,
    ai_context: AiCallContext | None = None,
) -> DetectRmbgDefectsResult:
    """detect-rmbg-defects core. Optional pre-composed bytes let a test or
    in-process caller (Phase 02 job 13) skip the recompose/re-fetch. `result_bytes`
    short-circuits the `result_crops[]` recompose; `original_bytes` the ORIGINAL
    compose/fetch."""
    t0 = time.monotonic()
    n_crops = len(req.crops)
    w_s = req.sheet_geometry.width
    h_s = req.sheet_geometry.height

    # 1. Prepare BOTH sheets IN PARALLEL, all SSRF-guarded:
    #      ORIGINAL = fast-path url OR compose `crops[]` (opaque)  → ALL_CROPS_FAILED
    #      RESULT   = fast-path url OR compose `result_crops[]` (RGBA transparent)
    #                 → ALL_RESULT_CROPS_FAILED
    #    Coordinate basis = sheet_geometry (taken HERE, before the downscale below);
    #    the composer guarantees both sheets are EXACTLY sheet_geometry.
    orig_raw, result_raw = await asyncio.gather(
        _prepare_original(req, original_bytes),
        _prepare_result(req, result_bytes),
    )

    # 2. Cost-downscale BOTH for Gemini (CPU-bound → to_thread). SAME
    #    SHEET_AI_MAX_EDGE → SAME out dims → stay pixel-aligned. ⚡ RESULT
    #    reencode='png' KEEPS alpha (RGBA in → RGBA PNG out); NEVER convert('RGB').
    #    Coordinates UNAFFECTED (0-1000→px uses sheet_geometry).
    try:
        ds = await asyncio.gather(
            asyncio.to_thread(
                downscale_for_ai_cost, orig_raw, max_edge=SHEET_AI_MAX_EDGE, reencode="png"
            ),
            asyncio.to_thread(
                downscale_for_ai_cost, result_raw, max_edge=SHEET_AI_MAX_EDGE, reencode="png"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — corrupt/undecodable sheet
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Could not decode an input image"
        ) from exc
    orig_fit = ds[0][0]
    result_fit = ds[1][0]

    # 3. Build prompt vars → render prompt + model.
    variables = {
        "image_guide": build_rmbg_detect_image_guide(),
        "cell_count": str(n_crops),
    }
    try:
        rendered_prompt, model = await load_and_render(
            DETECT_RMBG_DEFECTS_SYSTEM_NAME,
            variables,
            default_model=DETECT_RMBG_DEFECTS_DEFAULT_MODEL,
        )
    except PromptTemplateNotFound as exc:
        raise RemixDomainError(
            status=500, code="PROMPT_TEMPLATE_NOT_FOUND",
            message="detect-rmbg-defects prompt missing — seed not applied?",
        ) from exc

    # 4. Hard-guard inline payload (keep RESULT alpha intact).
    orig_fit, result_fit = await _fit_rmbg_detect_payload(orig_fit, result_fit, rendered_prompt)

    # 5. Gemini call (1×, structured JSON defects). Contract order: [orig, result];
    #    result = the LAST/inspection image (RGBA transparent).
    image_parts = [_img_part(orig_fit, "image/png"), _img_part(result_fit, "image/png")]
    gemini_t0 = time.monotonic()
    defects_raw, token = await _run_detect_gemini(
        rendered_prompt, image_parts, model, ai_context=ai_context
    )
    gemini_ms = int((time.monotonic() - gemini_t0) * 1000)

    # 6. Map → circles + filter + sort + cap (shared engine, RMBG categories).
    defects, raw_count, truncated = map_defects_to_circles(
        defects_raw, w_s, h_s,
        severity_threshold=req.severity_threshold,
        max_defects=req.max_defects,
    )

    # 7. Assign `cell` SERVER-SIDE by hit-testing the center against crops[].geometry.
    _assign_cells(defects, req.crops)

    meta = DetectRmbgDefectsMeta(
        cellCount=n_crops,
        defectCount=len(defects),
        rawDefectCount=raw_count,
        truncated=truncated or None,
        swappedDimensions=SwappedDimensions(width=w_s, height=h_s),
        processingTimeMs=int((time.monotonic() - t0) * 1000),
        tokenUsage=token,
    )
    logger.info(
        "detect_rmbg_defects_done W_s=%d H_s=%d cells=%d raw=%d defects=%d truncated=%s gemini_ms=%d",
        w_s, h_s, n_crops, raw_count, len(defects), truncated, gemini_ms,
    )
    return DetectRmbgDefectsResult(defects=defects, meta=meta)
