"""Stateless composer for /api/remix/build-crop-sheet.

Fetch crops in parallel (SSRF-guarded), decode sequentially in worker threads
(KISS — peak RAM stays low), composite on a gutter-filled RGBA canvas, draw
optional outer-bbox stroke around every placement (including skipped slots),
encode to PNG. Per-crop failures land in `skipped[]`; total failure raises
`RemixDomainError(ALL_CROPS_FAILED, 422)`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from io import BytesIO
from pathlib import Path
from time import monotonic_ns
from typing import Optional

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

# Importing image_ops triggers `Image.MAX_IMAGE_PIXELS` + `LOAD_TRUNCATED_IMAGES` setup.
from src.services import image_ops  # noqa: F401  (module-import side effect)
from src.models.requests.build_crop_sheet import (
    DEFAULT_FRAME_CELL_STROKE_COLOR,
    DEFAULT_FRAME_CELL_STROKE_WIDTH,
    DEFAULT_FRAME_DRAW_ORDINALS,
    DEFAULT_FRAME_GUTTER_COLOR,
    DEFAULT_FRAME_ORDINAL_BG_COLOR,
    DEFAULT_FRAME_ORDINAL_COLOR,
    DEFAULT_FRAME_ORDINAL_FONT_PX,
    BuildCropSheetRequest,
    Crop,
    SkippedCrop,
)
from src.services.http_fetch import fetch_image_bytes
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)

# Ordinal badge tuning (Validation S1 → top-gutter inversion 2026-06-30).
_MAX_BADGE_H = 50  # hard cap on badge HEIGHT — gutter trên 64px > 50 ⇒ badge không đè art ô dưới
_BADGE_PAD = 4  # padding text → badge edge (mỗi cạnh)
_BADGE_RADIUS = 4
_MIN_ORDINAL_FONT_PX = 8  # sàn khi giảm font cục bộ cho vừa cap

# Vendored bold TTF — default PIL bitmap font không scale theo font_px.
# parents[3] = ai-storybook-image-api/ (file: src/services/remix/crop_sheet_composer.py).
_FONT_PATH = (
    Path(__file__).resolve().parents[3] / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
)
# Cache loaded fonts theo px (tránh load TTF mỗi request). Module-level, an toàn:
# `_compose_sync` chạy trong thread pool nhưng dict read/write ở đây idempotent.
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_FONT_FALLBACK_WARNED = False


def _load_ordinal_font(font_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load+cache bold TTF tại `font_px`. Fallback `load_default` + warn nếu thiếu."""
    cached = _FONT_CACHE.get(font_px)
    if cached is not None:
        return cached
    global _FONT_FALLBACK_WARNED
    try:
        font = ImageFont.truetype(str(_FONT_PATH), font_px)
    except OSError:
        if not _FONT_FALLBACK_WARNED:
            logger.warning(
                "ordinal_font_fallback path=%s missing — using PIL default (no scale)",
                _FONT_PATH,
            )
            _FONT_FALLBACK_WARNED = True
        font = ImageFont.load_default()
    _FONT_CACHE[font_px] = font
    return font


def _measure_text_h(
    draw: "ImageDraw.ImageDraw",
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _fit_ordinal_font(
    draw: "ImageDraw.ImageDraw", label: str, font_px: int, max_badge_h: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Trả font sao cho `text_h + 2*pad ≤ max_badge_h`.

    `max_badge_h` = `min(_MAX_BADGE_H, gutter khả dụng)` ⇒ badge vừa gutter trên,
    KHÔNG đè art ô. Badge nở ngang phía trên ô (width KHÔNG cap) nên chỉ co theo
    chiều cao. Bắt đầu ở `font_px`, giảm dần (−2px) tới sàn `_MIN_ORDINAL_FONT_PX`.
    Fallback bitmap font (load_default) không scale → trả luôn (không giảm vô ích).
    Có thể vẫn vượt `max_badge_h` nếu chạm sàn font — caller phải clamp box (không
    dựa hoàn toàn vào shrink).
    """
    px = font_px
    font = _load_ordinal_font(px)
    if not isinstance(font, ImageFont.FreeTypeFont):
        return font  # bitmap fallback — size cố định, không shrink được
    while px > _MIN_ORDINAL_FONT_PX:
        if _measure_text_h(draw, label, font) + 2 * _BADGE_PAD <= max_badge_h:
            return font
        px -= 2
        font = _load_ordinal_font(px)
    return font


@dataclasses.dataclass(slots=True, frozen=True)
class ComposeCropSheetResult:
    png_bytes: bytes
    width: int
    height: int
    composed_count: int
    skipped: list[SkippedCrop]
    fetch_ms: int
    composite_ms: int
    input_bytes: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_hex_color(hex_str: str) -> tuple[int, int, int, int]:
    """`#RRGGBB` → (r, g, b, 255). Pydantic already validated shape."""
    return (
        int(hex_str[1:3], 16),
        int(hex_str[3:5], 16),
        int(hex_str[5:7], 16),
        255,
    )


_MAX_SKIPPED_MSG_LEN = 200


def _extract_fetch_error_msg(exc: HTTPException) -> str:
    """Best-effort pull of the spec-shaped `error.message` from http_fetch.

    Capped to `_MAX_SKIPPED_MSG_LEN` chars to keep `skipped[].message` bounded
    even if upstream changes shape (avoids leaking long detail dicts).
    """
    detail = exc.detail
    msg: str
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            msg = err["message"]
        else:
            msg = str(detail)
    else:
        msg = str(detail)
    return msg[:_MAX_SKIPPED_MSG_LEN]


async def _fetch_one(crop: Crop) -> tuple[Optional[bytes], Optional[SkippedCrop]]:
    """Fetch one crop. On any failure → (None, SkippedCrop(FETCH_ERROR))."""
    try:
        bytes_, _ctype = await fetch_image_bytes(crop.media_url)
        return bytes_, None
    except HTTPException as exc:
        return None, SkippedCrop(
            id=crop.id,
            reason="FETCH_ERROR",
            message=_extract_fetch_error_msg(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return None, SkippedCrop(
            id=crop.id,
            reason="FETCH_ERROR",
            message=f"unexpected {type(exc).__name__}",
        )


def _decode_one(bytes_: bytes) -> Optional[Image.Image]:
    """Sync decode → RGBA. Returns None on failure. Caller wraps in to_thread."""
    try:
        img = Image.open(BytesIO(bytes_))
        img.load()  # force decode; honours LOAD_TRUNCATED_IMAGES=False
        return img.convert("RGBA")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        return None
    except Exception:  # noqa: BLE001
        return None


async def _decode_all(
    fetch_results: list[tuple[Optional[bytes], Optional[SkippedCrop]]],
    crops: list[Crop],
) -> list[tuple[Optional[Image.Image], Optional[SkippedCrop]]]:
    """Sequential decode per crop (KISS — low peak RAM)."""
    out: list[tuple[Optional[Image.Image], Optional[SkippedCrop]]] = []
    for crop, (bytes_, skip) in zip(crops, fetch_results, strict=True):
        if skip is not None or bytes_ is None:
            out.append((None, skip))
            continue
        img = await asyncio.to_thread(_decode_one, bytes_)
        if img is None:
            out.append(
                (
                    None,
                    SkippedCrop(
                        id=crop.id,
                        reason="DECODE_ERROR",
                        message="Unable to decode image",
                    ),
                )
            )
        else:
            out.append((img, None))
    return out


def _compose_sync(
    req: BuildCropSheetRequest,
    items: list[tuple[Optional[Image.Image], Optional[SkippedCrop]]],
    fit_mode: str = "stretch",
    transparent_canvas: bool = False,
) -> tuple[bytes, int, int]:
    """Build canvas + composite + stroke + PNG encode. Fully blocking.

    `fit_mode` controls how each image is placed into its slot geometry:
      - "stretch" (default — crop-sheet behaviour, byte-parity with 01/04):
        resize exactly to (g.w, g.h). Crop-sheet slots already match the source
        aspect (layout engine), so stretch is a no-op distortion-wise.
      - "contain" (⚡rev6 variant sheets): keep aspect, scale to fit inside the
        slot (may upscale up to the slot edge, never beyond), center; the
        surrounding slack stays gutter-colored.

    `transparent_canvas` (⚡rmbg detect 08): when True the base canvas is RGBA
    `(0,0,0,0)` (fully transparent) instead of the opaque gutter fill, so the
    gutter + every empty cell stay alpha 0 and each RGBA piece's alpha is
    PRESERVED through `alpha_composite` (src over a transparent dst = src). The
    default (False) keeps the opaque gutter fill — NO behaviour change for the
    existing crop-sheet (01/04) + variant-sheet (mix 07) callers. Callers that
    need an alpha-clean result also pass a PLAIN frame (cell_stroke_width=0,
    draw_ordinals=False) so no opaque stroke/badge is baked over the alpha.
    """
    W = req.sheet_geometry.width
    H = req.sheet_geometry.height
    frame = req.frame

    gutter_hex = (frame.gutter_color if frame and frame.gutter_color else DEFAULT_FRAME_GUTTER_COLOR)
    stroke_hex = (
        frame.cell_stroke_color if frame and frame.cell_stroke_color else DEFAULT_FRAME_CELL_STROKE_COLOR
    )
    stroke_w = (
        frame.cell_stroke_width
        if frame is not None and frame.cell_stroke_width is not None
        else DEFAULT_FRAME_CELL_STROKE_WIDTH
    )

    # Ordinal resolve — `draw_ordinals` dùng `is not None` để giữ `False` (không
    # bị truthy check nuốt thành default True).
    draw_ordinals = (
        frame.draw_ordinals
        if frame is not None and frame.draw_ordinals is not None
        else DEFAULT_FRAME_DRAW_ORDINALS
    )
    ordinal_hex = (
        frame.ordinal_color if frame and frame.ordinal_color else DEFAULT_FRAME_ORDINAL_COLOR
    )
    ordinal_bg_hex = (
        frame.ordinal_bg_color if frame and frame.ordinal_bg_color else DEFAULT_FRAME_ORDINAL_BG_COLOR
    )
    ordinal_font_px = (
        frame.ordinal_font_px
        if frame is not None and frame.ordinal_font_px is not None
        else DEFAULT_FRAME_ORDINAL_FONT_PX
    )

    gutter_rgba = _parse_hex_color(gutter_hex)
    stroke_rgba = _parse_hex_color(stroke_hex)

    # MAX_SHEET_PIXELS (32M) stays below `Image.MAX_IMAGE_PIXELS` (50M, set in
    # `services/image_ops.py`) so canvas creation never trips the bomb guard.
    # transparent_canvas → alpha-0 base (rmbg detect 08, alpha-preserving); else
    # the opaque gutter fill (default — crop/variant sheet parity).
    canvas = Image.new(
        "RGBA", (W, H), (0, 0, 0, 0) if transparent_canvas else gutter_rgba
    )

    # Pass 1: composite each available crop.
    # Outer try/finally guarantees any decoded image we didn't get to close
    # (e.g. if Pillow raises mid-loop) is still released.
    try:
        for idx, (crop, (img, _skip)) in enumerate(zip(req.crops, items, strict=True)):
            if img is None:
                continue
            g = crop.geometry
            try:
                if fit_mode == "contain":
                    # Fit-contain: keep aspect, center inside the slot.
                    src_w, src_h = img.size
                    scale = min(g.w / src_w, g.h / src_h)
                    new_w = max(1, round(src_w * scale))
                    new_h = max(1, round(src_h * scale))
                    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    dest = (g.x + (g.w - new_w) // 2, g.y + (g.h - new_h) // 2)
                else:
                    resized = img.resize((g.w, g.h), Image.Resampling.LANCZOS)
                    dest = (g.x, g.y)
                try:
                    canvas.alpha_composite(resized, dest=dest)
                finally:
                    resized.close()
            finally:
                img.close()
                # Mark as closed so the finally-block below doesn't double-close.
                items[idx] = (None, _skip)
    finally:
        for img, _s in items:
            if img is not None:
                try:
                    img.close()
                except Exception:  # noqa: BLE001
                    pass

    # Pass 2: stroke every cell (regardless of skipped) on outer bbox
    draw: Optional[ImageDraw.ImageDraw] = None
    if stroke_w > 0:
        draw = ImageDraw.Draw(canvas)
        for crop in req.crops:
            g = crop.geometry
            x0 = max(0, g.x - stroke_w)
            y0 = max(0, g.y - stroke_w)
            x1 = min(W - 1, g.x + g.w + stroke_w - 1)
            y1 = min(H - 1, g.y + g.h + stroke_w - 1)
            draw.rectangle((x0, y0, x1, y1), outline=stroke_rgba, width=stroke_w)

    # Pass 5b: bake ordinal badge (index+1) cho MỌI ô (kể cả skipped), SAU stroke
    # ⇒ badge nổi trên cùng. Số khớp paste order + thứ tự skipped[] ⇒ số bake luôn
    # trùng số preview FE.
    #
    # KHÔNG đè art (yêu cầu cứng): art ô = rect [g.x, g.x+g.w) × [g.y, g.y+g.h).
    # Badge đặt STRICTLY trong gutter TRÊN ô (bottom edge < g.y, căn cạnh trái ô);
    # nếu gutter trên hẹp → fallback gutter TRÁI (left of the cell, right edge < g.x).
    # Badge_h cap theo gutter khả dụng (font shrink cục bộ) ⇒ không tràn vào art.
    if draw_ordinals:
        if draw is None:
            draw = ImageDraw.Draw(canvas)
        ordinal_rgba = _parse_hex_color(ordinal_hex)
        ordinal_bg_rgba = _parse_hex_color(ordinal_bg_hex)
        for idx, crop in enumerate(req.crops):
            label = str(idx + 1)
            g = crop.geometry
            left_gutter = g.x  # px trống bên trái art (canvas..art.left)
            top_gutter = g.y  # px trống phía trên art (canvas..art.top)

            # Cap badge HEIGHT theo gutter khả dụng (≤ _MAX_BADGE_H) → shrink font
            # cho vừa, badge không lấn art ô. Width KHÔNG cap (badge nở ngang phía
            # trên ô; gutter ngang chỉ 8px nhưng badge nằm TRÊN ô nên không bị chặn).
            max_h = min(_MAX_BADGE_H, max(top_gutter, left_gutter))
            font = _fit_ordinal_font(draw, label, ordinal_font_px, max_h)
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            bw = text_w + 2 * _BADGE_PAD
            bh = min(text_h + 2 * _BADGE_PAD, _MAX_BADGE_H)

            if bh <= top_gutter:
                # Gutter trên đủ cao → đặt phía trên art, căn cạnh trái ô. KHÔNG đè.
                bx = max(0, min(g.x, W - bw))
                by = g.y - bh
            elif bw <= left_gutter:
                # Gutter trên hẹp nhưng gutter trái đủ → đặt bên trái art, top-aligned.
                bx = g.x - bw
                by = max(0, min(g.y, H - bh))
            else:
                # Cả hai gutter đều hẹp (payload không chừa margin) → ưu tiên gutter
                # lớn hơn, clamp trong canvas. Best-effort: vẫn cố tránh art tối đa.
                if top_gutter >= left_gutter:
                    bx = max(0, min(g.x, W - bw))
                    by = max(0, g.y - bh)
                else:
                    bx = max(0, g.x - bw)
                    by = max(0, min(g.y, H - bh))

            # PIL rectangle vẽ INCLUSIVE 2 mép → dùng (bx+bw-1, by+bh-1) để box
            # rộng đúng bw×bh px và right/bottom edge KHÔNG chạm cột/đầu art.
            draw.rounded_rectangle(
                (bx, by, bx + bw - 1, by + bh - 1),
                radius=_BADGE_RADIUS,
                fill=ordinal_bg_rgba,
            )
            # textbbox lệch origin (bbox[0]/bbox[1]) → trừ để text khít padding.
            draw.text(
                (bx + _BADGE_PAD - bbox[0], by + _BADGE_PAD - bbox[1]),
                label,
                font=font,
                fill=ordinal_rgba,
            )

    buf = BytesIO()
    canvas.save(buf, format="PNG", optimize=True, compress_level=6)
    canvas.close()
    return buf.getvalue(), W, H


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def compose_crop_sheet(
    req: BuildCropSheetRequest, *, transparent_canvas: bool = False
) -> ComposeCropSheetResult:
    """Compose a crop sheet from client-supplied geometry. Stateless.

    `transparent_canvas=True` (rmbg detect 08) builds the sheet on an alpha-0
    canvas so RGBA pieces keep their transparency (gutter + empty cells stay
    transparent); default keeps the opaque gutter fill (no regression to 01/04/07).
    """
    # --- Fetch parallel ---
    fetch_t0 = monotonic_ns()
    fetch_results = await asyncio.gather(*[_fetch_one(c) for c in req.crops])
    fetch_ms = (monotonic_ns() - fetch_t0) // 1_000_000

    input_bytes = sum(len(b) for (b, _s) in fetch_results if b is not None)

    # --- Decode (sequential, in threads) ---
    items = await _decode_all(fetch_results, req.crops)

    # Snapshot counters BEFORE _compose_sync — it consumes (closes) PIL images.
    skipped = [s for (img, s) in items if s is not None]
    composed_count = sum(1 for (img, _s) in items if img is not None)

    # Short-circuit: don't waste a composite pass on an all-skipped sheet.
    if composed_count == 0:
        raise RemixDomainError(
            status=422,
            code="ALL_CROPS_FAILED",
            message=f"All {len(req.crops)} crops failed to fetch/decode",
            details={"skipped": [s.model_dump() for s in skipped]},
        )

    # --- Composite ---
    comp_t0 = monotonic_ns()
    png_bytes, W, H = await asyncio.to_thread(
        _compose_sync, req, items, "stretch", transparent_canvas
    )
    composite_ms = (monotonic_ns() - comp_t0) // 1_000_000

    logger.info(
        "crop_sheet_composed n_crops=%d composed=%d skipped=%d fetch_ms=%d "
        "composite_ms=%d sheet_w=%d sheet_h=%d input_bytes=%d",
        len(req.crops),
        composed_count,
        len(skipped),
        fetch_ms,
        composite_ms,
        W,
        H,
        input_bytes,
    )

    return ComposeCropSheetResult(
        png_bytes=png_bytes,
        width=W,
        height=H,
        composed_count=composed_count,
        skipped=skipped,
        fetch_ms=fetch_ms,
        composite_ms=composite_ms,
        input_bytes=input_bytes,
    )
