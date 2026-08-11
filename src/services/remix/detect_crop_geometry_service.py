"""detect-crop-geometry core (Step-2 Gemini classify + orchestration).

In-process core `run_detect_crop_geometry` so the job cut pipeline (jobs 02/05) can
call it directly (no HTTP self-call) — the router is a thin wrapper. Flow: fetch the
2 sheets (or accept pre-fetched bytes from the job) → Step-1 numpy
`detect_frames_anchored` (ANCHORED per-cell: one box per crop, snapped in a bounded
window around the geometry-scaled expected position) → Step-2 Gemini classify
(frame_index → number; still catches reorder by scene content) → map (box = the
detected frame VERBATIM, no ratio reshape) → graceful multi-tier fallback.

Graceful tiers:
  - frames empty → detections=[], notFound=all (NOT an error).
  - Gemini fail (LLM/safety/parse) AND frames non-empty → positional fallback,
    `meta.degraded=true`, source='positional_fallback'.
  - Gemini fail AND frames empty → raise (router maps to envelope; job catches → static).

`@traceable` is on the SERVICE fn (a route-level `@traceable` is a no-op); the
per-API `run_name` is set inside `ainvoke(config=...)` (memory
`reference_traceable_route_handler_bypass`). PII discipline: never log URLs / bytes /
recognition_hint — only counts + dims at INFO/DEBUG.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import math
import time
from typing import Any

from langchain_core.messages import HumanMessage
from langsmith import traceable

from src.models.requests.detect_crop_geometry import (
    ASSIGNMENTS_SCHEMA,
    DETECT_CROP_GEOMETRY_DEFAULT_MODEL,
    DETECT_CROP_GEOMETRY_SYSTEM_NAME,
    DETECT_TEMPERATURE,
    DETECT_TIMEOUT_S,
    MAX_CROPS,
    MAX_DETECT_RETRIES,
    MAX_IMAGE_BYTES,
    CropBox,
    CropDetection,
    CropGeometry,
    DetectCropGeometryMeta,
    DetectCropGeometryRequest,
    OriginalCrop,
    SheetDimensions,
)
from src.services import http_fetch, image_ops
from src.services.ai_usage import AiCallContext
from src.services.gemini.invoke import gemini_ainvoke
from src.services.gemini.response import classify_gemini_exc
from src.services.prompt_loader import PromptTemplateNotFound, load_and_render
from src.services.remix.errors import RemixDomainError
from src.services.image.frame_finder import FrameBox, detect_frames_anchored

logger = logging.getLogger(__name__)

__all__ = [
    "DetectCropGeometryResult",
    "build_crop_guide",
    "build_candidate_list",
    "positional_assign",
    "run_classify_gemini",
    "run_detect_crop_geometry",
    "detect_boxes_for_cut",
]

_RAW_LOG_CAP = 160
_SAFETY_FINISH_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}
_POSITIONAL_SIZE_TOL = 0.35  # loose size gate on the degraded path

# Concurrency gate for the Step-2 classify call. SEPARATE from the image-preview
# `_gemini_sem` (gemini_image_seams) on purpose: classify is a cheaper TEXT/vision
# model (`gemini-3.5-flash`) on a different upstream rate-limit pool, so it must NOT
# compete for the 3 image-preview slots. This bounds the +1 classify call/sheet that
# the cut-delegate adds across N concurrent remix jobs (graceful 429 → positional
# fallback anyway, but governance keeps cost/latency bounded).
_CLASSIFY_CONCURRENCY_CAP = 3
_CLASSIFY_SEM = asyncio.Semaphore(_CLASSIFY_CONCURRENCY_CAP)
_REINFORCE = (
    "\n\nNHẮC LẠI: chỉ trả JSON đúng schema "
    '{ "assignments": [ { "frame_index": <int>, "number": <int>, "confidence": <0..1> } ] }. '
    "frame_index trong danh sách khung; number trong bản đồ ô gốc. KHÔNG văn xuôi."
)


# ─── Result ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass(slots=True)
class DetectCropGeometryResult:
    """Core output — router builds the response envelope, job reads `.detections`."""

    detections: list[CropDetection]
    meta: DetectCropGeometryMeta


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


def _extract_token(response: Any) -> int | None:
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


def _parse_assignments(raw_text: str, *, final: bool) -> list[dict]:
    """Tolerant parse → list of `{frame_index:int, number:int, confidence:float|None}`.

    Accepts `{assignments:[...]}` or a bare array. Individually-malformed items are
    dropped; globally malformed (non-JSON / no array) → `_ParseRetry` on the first
    attempt, `RemixDomainError(500, PARSE_ERROR)` on the final attempt.
    """
    cleaned = _strip_fence(raw_text)

    def _bad(msg: str):
        logger.warning("detect_classify_parse_invalid final=%s raw=%s", final, cleaned[:_RAW_LOG_CAP])
        if final:
            return RemixDomainError(status=500, code="PARSE_ERROR", message=msg)
        return _ParseRetry(msg)

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise _bad("Gemini returned non-JSON assignments") from exc

    items: Any = obj
    if isinstance(obj, dict):
        if isinstance(obj.get("assignments"), list):
            items = obj["assignments"]
        else:
            raise _bad("Response object has no assignments array")
    if not isinstance(items, list):
        raise _bad("Expected a JSON array of assignments")

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fi = item.get("frame_index")
        num = item.get("number")
        if isinstance(fi, bool) or isinstance(num, bool):
            continue
        if not isinstance(fi, int) or not isinstance(num, int):
            continue
        conf = item.get("confidence")
        confidence = (
            max(0.0, min(1.0, float(conf)))
            if isinstance(conf, (int, float)) and not isinstance(conf, bool)
            else None
        )
        out.append({"frame_index": fi, "number": num, "confidence": confidence})
    return out


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
    safety exc to `RemixDomainError` (502 LLM_ERROR / 504 → 502 / 422 safety)."""
    try:
        async with _CLASSIFY_SEM:  # govern classify concurrency (separate pool)
            result = await gemini_ainvoke(
                model=model,
                messages=[HumanMessage(content=content_parts)],
                run_name="remix_detect_crop_geometry",
                timeout_s=DETECT_TIMEOUT_S,
                temperature=DETECT_TEMPERATURE,
                response_mime_type="application/json",
                response_schema=ASSIGNMENTS_SCHEMA,
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
        logger.warning("detect_classify_gemini_error code=%s err=%s", code, str(exc)[:_RAW_LOG_CAP])
        raise RemixDomainError(status=502, code="LLM_ERROR", message="Gemini request failed") from exc


@traceable(name="remix.detect_crop_geometry.gemini", run_type="llm")
async def run_classify_gemini(
    rendered_prompt: str,
    original_part: dict,
    swapped_part: dict,
    model: str,
    *,
    ai_context: AiCallContext | None = None,
) -> tuple[list[dict], int | None]:
    """Gemini classify (frame_index → number). Parts order = Ảnh #1 (original) then
    Ảnh #2 (swapped). 1 app-level parse-retry with reinforcement; langchain
    `max_retries` covers transient transport. Raises `RemixDomainError` on
    safety/LLM/parse failure. `ai_context` (Phase 05) attributes each Gemini call."""
    base_parts = [original_part, swapped_part]
    resp = await _invoke_gemini(
        model, [{"type": "text", "text": rendered_prompt}, *base_parts],
        ai_context=ai_context,
    )
    _raise_if_safety_blocked(resp)
    token = _extract_token(resp)
    text = _extract_text(resp.content)
    # First attempt: non-empty content that parses cleanly returns immediately;
    # empty content OR a globally-malformed body falls through to one retry.
    if text.strip():
        try:
            return _parse_assignments(text, final=False), token
        except _ParseRetry:
            pass

    # Retry once with reinforcement.
    resp2 = await _invoke_gemini(
        model, [{"type": "text", "text": rendered_prompt + _REINFORCE}, *base_parts],
        ai_context=ai_context,
    )
    _raise_if_safety_blocked(resp2)
    token2 = _extract_token(resp2)
    text2 = _extract_text(resp2.content)
    if not text2.strip():
        raise RemixDomainError(status=500, code="PARSE_ERROR", message="Gemini returned empty assignments")
    assignments = _parse_assignments(text2, final=True)
    return assignments, (token or 0) + (token2 or 0) or None


# ─── Guide builders ──────────────────────────────────────────────────────────


def build_crop_guide(crops: list[OriginalCrop], dims_w: int, dims_h: int) -> str:
    """Số ô gốc → vị trí % trên Ảnh #1 (+hint). Sorted by number ascending."""
    lines = ["## Ô gốc trên Ảnh #1 — toạ độ % (gốc trên-trái = 0%,0%)"]
    for c in sorted(crops, key=lambda c: c.number):
        g = c.geometry
        cx = round(100 * g.x / dims_w) if dims_w else 0
        cy = round(100 * g.y / dims_h) if dims_h else 0
        cw = round(100 * g.w / dims_w) if dims_w else 0
        ch = round(100 * g.h / dims_h) if dims_h else 0
        hint = f" — gợi ý cảnh: {c.recognition_hint}" if c.recognition_hint else ""
        lines.append(
            f"- Ô số {c.number}: góc trên-trái ~({cx}%,{cy}%), rộng ~{cw}%, cao ~{ch}%{hint}"
        )
    return "\n".join(lines)


def build_candidate_list(frames: list[FrameBox], w_s: int, h_s: int) -> str:
    """Khung Step-1 → vị trí%+size% trên Ảnh #2. `frames` already row-major sorted."""
    lines = ["## Khung đã tìm trên Ảnh #2 — toạ độ % (gốc trên-trái = 0%,0%)"]
    for i, f in enumerate(frames):
        fx = round(100 * f.x / w_s) if w_s else 0
        fy = round(100 * f.y / h_s) if h_s else 0
        fw = round(100 * f.w / w_s) if w_s else 0
        fh = round(100 * f.h / h_s) if h_s else 0
        lines.append(f"- Khung {i}: góc trên-trái ~({fx}%,{fy}%), rộng ~{fw}%, cao ~{fh}%")
    return "\n".join(lines)


# ─── Positional fallback (degraded) ──────────────────────────────────────────


def positional_assign(
    frames: list[FrameBox],
    crops: list[OriginalCrop],
    sx: float,
    sy: float,
    sheet_wh: tuple[int, int],
) -> list[dict]:
    """Greedy nearest-center assignment when Gemini fails. Each frame → the nearest
    cell center (`geometry × scale`) passing a loose size gate; each number ≤1,
    each frame ≤1. confidence = low proximity score. Loses reorder detection (degraded)."""
    w_s, h_s = sheet_wh
    diag = math.hypot(w_s, h_s) or 1.0
    pairs: list[tuple[float, int, int]] = []
    for fi, f in enumerate(frames):
        fcx, fcy = f.x + f.w / 2, f.y + f.h / 2
        for c in crops:
            g = c.geometry
            ew, eh = g.w * sx, g.h * sy
            if ew <= 0 or eh <= 0:
                continue
            if max(abs(f.w - ew) / ew, abs(f.h - eh) / eh) > _POSITIONAL_SIZE_TOL:
                continue
            ecx = (g.x + g.w / 2) * sx
            ecy = (g.y + g.h / 2) * sy
            pairs.append((math.hypot(fcx - ecx, fcy - ecy), fi, c.number))
    pairs.sort(key=lambda p: p[0])
    used_f: set[int] = set()
    used_n: set[int] = set()
    out: list[dict] = []
    for dist, fi, num in pairs:
        if fi in used_f or num in used_n:
            continue
        used_f.add(fi)
        used_n.add(num)
        conf = max(0.05, min(0.5, 0.5 * (1.0 - dist / diag)))
        out.append({"frame_index": fi, "number": num, "confidence": conf})
    return out


# ─── Fetch ───────────────────────────────────────────────────────────────────


async def _fetch_sheet(url: str) -> tuple[bytes, str]:
    # `fetch_image_bytes` already SSRF-validates the URL + every redirect hop and
    # caps size/mime; any failure (SSRF block, fetch, bad mime) → 422 IMAGE_FETCH_ERROR
    # (the design has no distinct SSRF code on this endpoint).
    try:
        return await http_fetch.fetch_image_bytes(url, max_bytes=MAX_IMAGE_BYTES, timeout_s=30.0)
    except RemixDomainError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Failed to fetch sheet image"
        ) from exc


# ─── Core ────────────────────────────────────────────────────────────────────


def _map_assignments_to_detections(
    assignments: list[dict],
    frames: list[FrameBox],
    crop_by_number: dict[int, OriginalCrop],
    source: str,
    default_conf: float,
) -> tuple[list[CropDetection], int]:
    """Dedup (highest confidence per frame/number); box = the detected frame
    VERBATIM (no ratio reshape). Returns (detections, used_frame_count)."""
    n_frames = len(frames)
    seen_f: set[int] = set()
    seen_n: set[int] = set()
    chosen: list[tuple[int, int, float | None]] = []
    for a in sorted(assignments, key=lambda a: -(a.get("confidence") or 0.0)):
        fi = a["frame_index"]
        num = a["number"]
        if not (0 <= fi < n_frames) or num not in crop_by_number:
            continue
        if fi in seen_f or num in seen_n:
            continue
        seen_f.add(fi)
        seen_n.add(num)
        chosen.append((fi, num, a.get("confidence")))

    detections: list[CropDetection] = []
    for fi, num, conf in chosen:
        # Box = the detected inner-of-stroke frame VERBATIM (actual content region
        # on the swap result). No ratio reshape: growing would over-crop the
        # border/gutter, shrinking would clip real content — the art lives entirely
        # inside the frame, so the detected box IS the crop.
        box = frames[fi]
        detections.append(
            CropDetection(
                number=num,
                box=CropBox(x=box.x, y=box.y, w=box.w, h=box.h),
                confidence=float(conf if conf is not None else default_conf),
                source=source,  # type: ignore[arg-type]
            )
        )
    return detections, len(chosen)


def _reorder_detected(
    detections: list[CropDetection], crop_by_number: dict[int, OriginalCrop]
) -> bool:
    """True when the (y,x) order of assigned boxes differs from the assigned numbers'
    original-grid (geometry y,x) order — i.e. Gemini detected a cell swap."""
    if len(detections) < 2:
        return False
    by_box = [d.number for d in sorted(detections, key=lambda d: (d.box.y, d.box.x))]
    by_grid = sorted(
        (d.number for d in detections),
        key=lambda n: (crop_by_number[n].geometry.y, crop_by_number[n].geometry.x),
    )
    return by_box != by_grid


async def run_detect_crop_geometry(
    req: DetectCropGeometryRequest,
    *,
    swapped_bytes: bytes | None = None,
    original_bytes: bytes | None = None,
    swapped_mime: str | None = None,
    original_mime: str | None = None,
    ai_context: AiCallContext | None = None,
) -> DetectCropGeometryResult:
    """detect-crop-geometry core. `swapped_bytes`/`original_bytes` let the in-process
    job caller skip the re-fetch (it already has the swapped sheet bytes). `ai_context`
    (Phase 05) attributes the Gemini classify call — the sync router builds it from
    `req.remixId`; the job cut path (`detect_boxes_for_cut`) leaves it None."""
    t0 = time.monotonic()
    crops = req.crops
    dims = req.original_sheet_dimensions
    requested_numbers = (
        [n for n in req.target_numbers if n in {c.number for c in crops}]
        if req.target_numbers is not None
        else [c.number for c in crops]
    )
    requested_set = set(requested_numbers)

    # 1. Fetch the sheets we don't already have (parallel).
    fetch_specs: list[tuple[str, str]] = []
    if swapped_bytes is None:
        fetch_specs.append(("swapped", str(req.swapped_sheet_url)))
    if original_bytes is None:
        fetch_specs.append(("original", str(req.original_sheet_url)))
    if fetch_specs:
        results = await asyncio.gather(*(_fetch_sheet(url) for _, url in fetch_specs))
        for (kind, _), (data, mime) in zip(fetch_specs, results):
            if kind == "swapped":
                swapped_bytes, swapped_mime = data, mime
            else:
                original_bytes, original_mime = data, mime
    assert swapped_bytes is not None and original_bytes is not None

    try:
        w_s, h_s = await asyncio.to_thread(image_ops.measure_size, swapped_bytes)
    except Exception as exc:  # noqa: BLE001 — corrupt/undecodable swapped sheet
        raise RemixDomainError(
            status=422, code="IMAGE_FETCH_ERROR", message="Could not decode swapped sheet"
        ) from exc
    sx = w_s / dims.width
    sy = h_s / dims.height

    # 2. Step-1 — ANCHORED per-cell frame detection (blocking → to_thread).
    # One box per crop, snapped in a bounded window around the geometry-scaled
    # expected position (replaces UNANCHORED topology — the 8px tight-gutter layout
    # merged whole rows into one blob → zero frames). `expected_boxes` is built in
    # `crops` order, but `detect_frames_anchored` returns a ROW-MAJOR candidate list
    # (frame_index is NOT crop index); Step-2 assigns each frame its TRUE number by
    # scene content — that content match is what catches a Gemini cell-reorder.
    # badge_bg=None → badge-landmark deferred (phase-04).
    expected_boxes = [
        (c.geometry.x * sx, c.geometry.y * sy, c.geometry.w * sx, c.geometry.h * sy)
        for c in crops
    ]
    frames = await asyncio.to_thread(
        detect_frames_anchored, swapped_bytes, expected_boxes, badge_bg=None
    )

    crop_by_number = {c.number: c for c in crops}

    def _build_meta(
        detections: list[CropDetection],
        frame_count: int,
        used_frames: int,
        degraded: bool,
        token: int | None,
        reorder: bool,
    ) -> DetectCropGeometryMeta:
        assigned = {d.number for d in detections}
        not_found = sorted(n for n in requested_set if n not in assigned)
        return DetectCropGeometryMeta(
            requestedCount=len(requested_set),
            detectedCount=len(detections),
            frameCount=frame_count,
            droppedFrames=max(0, frame_count - used_frames),
            notFound=not_found or None,
            reorderDetected=reorder or None,
            degraded=degraded or None,
            processingTimeMs=int((time.monotonic() - t0) * 1000),
            tokenUsage=token,
        )

    # Graceful tier 1 — no frames found.
    if not frames:
        logger.info(
            "detect_crop_geometry_no_frames W_s=%d H_s=%d crops=%d", w_s, h_s, len(crops)
        )
        return DetectCropGeometryResult(
            detections=[],
            meta=_build_meta([], 0, 0, degraded=False, token=None, reorder=False),
        )

    # 3-6. Build prompt + Step-2 Gemini classify.
    crop_guide = build_crop_guide(crops, dims.width, dims.height)
    candidate_list = build_candidate_list(frames, w_s, h_s)
    try:
        rendered, model = await load_and_render(
            DETECT_CROP_GEOMETRY_SYSTEM_NAME,
            {
                "crop_guide": crop_guide,
                "candidate_frames": candidate_list,
                "crop_count": str(len(crops)),
                "frame_count": str(len(frames)),
            },
            default_model=DETECT_CROP_GEOMETRY_DEFAULT_MODEL,
        )
    except PromptTemplateNotFound as exc:
        raise RemixDomainError(
            status=500, code="PROMPT_TEMPLATE_NOT_FOUND", message="detect-crop-geometry prompt missing"
        ) from exc

    original_part = _img_part(original_bytes, original_mime or "image/png")
    swapped_part = _img_part(swapped_bytes, swapped_mime or "image/png")

    degraded = False
    token: int | None = None
    try:
        assignments, token = await run_classify_gemini(
            rendered, original_part, swapped_part, model, ai_context=ai_context
        )
        source = "semantic"
        default_conf = 0.5
    except RemixDomainError:
        # Graceful tier 2 — Gemini failed but we have frames → positional fallback.
        logger.warning("detect_crop_geometry_degraded frames=%d", len(frames))
        assignments = positional_assign(frames, crops, sx, sy, (w_s, h_s))
        source = "positional_fallback"
        default_conf = 0.2
        degraded = True

    detections, used_frames = _map_assignments_to_detections(
        assignments, frames, crop_by_number, source, default_conf
    )

    # Filter to target_numbers (when supplied) — reorder uses the full assignment set.
    reorder = _reorder_detected(detections, crop_by_number)
    if req.target_numbers is not None:
        detections = [d for d in detections if d.number in requested_set]

    logger.info(
        "detect_crop_geometry_done W_s=%d H_s=%d crops=%d frames=%d detections=%d "
        "degraded=%s reorder=%s",
        w_s, h_s, len(crops), len(frames), len(detections), degraded, reorder,
    )
    return DetectCropGeometryResult(
        detections=detections,
        meta=_build_meta(detections, len(frames), used_frames, degraded, token, reorder),
    )


# ─── Job-facing bridge (in-process delegate for the cut pipeline) ────────────


async def detect_boxes_for_cut(
    swapped_bytes: bytes,
    *,
    sheet_geometry: dict[str, Any],
    crops: list[dict[str, Any]],
    original_sheet_url: str,
    swapped_sheet_url: str,
    recognition_hints: list[str | None] | None = None,
    sheet_idx: int = 0,
) -> dict[int, tuple[int, int, int, int]]:
    """Job → detect-core bridge. Builds a `DetectCropGeometryRequest` from the cut
    crop dicts (number = index+1, geometry from `crop['geometry']`), calls the core
    IN-PROCESS (passing `swapped_bytes` to skip the re-fetch), and returns
    `{crop_index: (x,y,w,h)}` (swapped px) for the cells the API located.

    NEVER raises — any failure (build/SSRF/fetch/LLM/decode) returns `{}` so the
    caller statically cuts every crop (graceful, never-worse). PII discipline:
    no URLs/bytes/hints logged.
    """
    if not crops:
        return {}
    # No silent truncation: detect caps crops at MAX_CROPS (prompt-bloat guard).
    # A sheet over the cap delegates NOTHING (all-static, never-worse) + logs it.
    if len(crops) > MAX_CROPS:
        logger.warning(
            "detect_boxes_for_cut_over_cap sheet_idx=%d crops=%d cap=%d -> all static",
            sheet_idx, len(crops), MAX_CROPS,
        )
        return {}
    hints = recognition_hints or [None] * len(crops)
    try:
        width = int(sheet_geometry.get("width") or 0)
        height = int(sheet_geometry.get("height") or 0)
        if width <= 0 or height <= 0:
            return {}
        original_crops: list[OriginalCrop] = []
        for i, c in enumerate(crops):
            g = c.get("geometry") or {}
            gx = max(0, int(g.get("x", 0)))
            gy = max(0, int(g.get("y", 0)))
            gw = max(1, int(g.get("w", 0)))
            gh = max(1, int(g.get("h", 0)))
            hint = hints[i] if i < len(hints) else None
            original_crops.append(
                OriginalCrop(
                    number=i + 1,
                    geometry=CropGeometry(x=gx, y=gy, w=gw, h=gh),
                    recognition_hint=(hint or None),
                )
            )
        req = DetectCropGeometryRequest(
            original_sheet_url=original_sheet_url,  # type: ignore[arg-type]
            swapped_sheet_url=swapped_sheet_url,  # type: ignore[arg-type]
            crops=original_crops,
            original_sheet_dimensions=SheetDimensions(width=width, height=height),
        )
        result = await run_detect_crop_geometry(req, swapped_bytes=swapped_bytes)
    except Exception as exc:  # noqa: BLE001 — detect is a quality boost, never fatal
        logger.warning(
            "detect_boxes_for_cut_skipped sheet_idx=%d err_type=%s", sheet_idx, type(exc).__name__
        )
        return {}

    box_by_index: dict[int, tuple[int, int, int, int]] = {}
    for d in result.detections:
        idx = d.number - 1
        if 0 <= idx < len(crops):
            box_by_index[idx] = (d.box.x, d.box.y, d.box.w, d.box.h)
    logger.info(
        "detect_boxes_for_cut sheet_idx=%d crops=%d located=%d degraded=%s",
        sheet_idx, len(crops), len(box_by_index), bool(result.meta.degraded),
    )
    return box_by_index
