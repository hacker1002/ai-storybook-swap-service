"""Pillow-based image operations (sync; caller wraps in asyncio.to_thread).

Ported from image-api `services/image_ops.py` — ONLY the self-contained helpers
the remix pipeline needs (composite/alpha, resize LANCZOS, PNG normalize, MIME
sniff, hex-flatten, dimension measure). The normalize-ratio block, SVG/GIF
dimension parsers, `crop_to_png_base64` (retouch), and `compose_on_canvas` /
`crop_fraction` (outpaint) are intentionally DROPPED — non-remix domain, and the
normalize-ratio helpers pull in a request-model module this service does not ship.

Module-level decompression-bomb guard + `LOAD_TRUNCATED_IMAGES = False` are kept
(importing this module sets them process-wide — remix helpers rely on the guard).
"""

import logging
from io import BytesIO

import numpy as np
from PIL import Image, ImageFile, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Decompression bomb guard (module-level)
Image.MAX_IMAGE_PIXELS = 50_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False


def composite_with_mask(
    original_bytes: bytes,
    mask_bytes: bytes,
) -> tuple[bytes, int, int, float]:
    """Apply mask as alpha channel to original image.

    Returns (rgba_png_bytes, source_width, source_height, coverage_ratio).
    coverage_ratio in [0, 1] = fraction of mask pixels with value > 0.
    """
    with Image.open(BytesIO(original_bytes)) as orig_ctx:
        orig = orig_ctx.convert("RGBA")
        src_w, src_h = orig.size

        with Image.open(BytesIO(mask_bytes)) as mask_ctx:
            mask = mask_ctx.convert("L")
            if mask.size != orig.size:
                logger.debug("mask_resize from=%s to=%s", mask.size, orig.size)
                mask = mask.resize(orig.size, Image.Resampling.LANCZOS)

            mask_arr = np.asarray(mask)
            coverage = float((mask_arr > 0).mean())

            orig.putalpha(mask)

        out = BytesIO()
        orig.save(out, format="PNG", optimize=True)
        return out.getvalue(), src_w, src_h, coverage


def sniff_mime(head: bytes) -> str | None:
    """Sniff MIME from first ≤256 bytes. Returns None if unrecognized."""
    if not head:
        return None

    # JPEG: FF D8 FF
    if len(head) >= 3 and head[0] == 0xFF and head[1] == 0xD8 and head[2] == 0xFF:
        return "image/jpeg"

    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # GIF: GIF87a / GIF89a
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"

    # WebP: RIFF....WEBP
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"

    return None


def measure_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) of an encoded image. Raise ValueError on decode fail.

    Blocking (Pillow decode). Caller must wrap in `asyncio.to_thread`. The
    module-level `Image.MAX_IMAGE_PIXELS` guard still applies (decompression
    bomb). Used by the upscale core to measure Replicate output dimensions.
    """
    try:
        with Image.open(BytesIO(data)) as img:
            return img.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        logger.warning("measure_size_decode_fail err=%s", exc)
        raise ValueError("DECODE_ERROR") from exc


def ensure_png(data: bytes, source_mime: str) -> bytes:
    """Re-encode image bytes to PNG if source is not already PNG.

    No-op fast path when source is already PNG. Blocking. Caller must wrap in
    asyncio.to_thread.
    """
    if source_mime == "image/png":
        return data
    with Image.open(BytesIO(data)) as img_ctx:
        img_ctx.load()
        # Preserve existing mode (RGB/RGBA/L). Pillow encodes all natively to PNG.
        buf = BytesIO()
        img_ctx.save(buf, format="PNG", optimize=True, compress_level=6)
        return buf.getvalue()


def hex_to_rgba(hex_str: str) -> tuple[int, int, int, int]:
    """Parse `#RGB` / `#RRGGBB` → opaque RGBA tuple (alpha=255).

    Expands 3-nibble shorthand (`#abc` → `aabbcc`) before parsing.
    Raises ValueError on malformed length or non-hex digits.
    """
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"invalid hex color length: {hex_str!r}")
    try:
        r, g, b = (int(s[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise ValueError(f"invalid hex color digits: {hex_str!r}") from exc
    return r, g, b, 255


def flatten_on_color(data: bytes, hex_color: str) -> bytes:
    """Composite an RGBA image over an opaque background color → PNG bytes.

    Decodes `data`, overlays it on a solid `hex_color` canvas via
    `alpha_composite`, and re-encodes PNG. Output background pixels become
    opaque (alpha=255). Self-contained PNG emit — caller must NOT also run
    `ensure_png` on the result. Blocking. Caller must wrap in asyncio.to_thread.
    """
    fill = hex_to_rgba(hex_color)
    with Image.open(BytesIO(data)) as im:
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, fill)
        flat = Image.alpha_composite(bg, rgba)

    buf = BytesIO()
    flat.save(buf, format="PNG", optimize=True, compress_level=6)
    return buf.getvalue()


# Re-export for callers
__all__ = [
    "composite_with_mask",
    "sniff_mime",
    "measure_size",
    "ensure_png",
    "hex_to_rgba",
    "flatten_on_color",
]
