"""Shared image helpers for remix swap services (mix + sprite crop-sheet swap).

The mix-swap and sprite-swap cores reuse the exact same aspect-ratio snapping,
output resize, and dated storage-path logic without duplication — all import
from here.

All Pillow work is synchronous (C-extension); callers wrap in
`asyncio.to_thread`.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from src.services import image_ops  # noqa: F401 — Pillow setup side-effects

__all__ = [
    "SUPPORTED_ASPECT_RATIOS",
    "snap_aspect_ratio",
    "resize_to",
    "ensure_png_native",
    "build_dated_path",
]

# Gemini-supported aspect_ratio labels — pick the closest match for `image_config`.
# (Other labels exist in the model API but stay within this conservative subset.)
SUPPORTED_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("2:3", 2 / 3),
    ("3:2", 3 / 2),
    ("3:4", 3 / 4),
    ("4:3", 4 / 3),
    ("4:5", 4 / 5),
    ("5:4", 5 / 4),
    ("9:16", 9 / 16),
    ("16:9", 16 / 9),
    ("21:9", 21 / 9),
)


def snap_aspect_ratio(width: int, height: int) -> str:
    """Pick the closest discrete Gemini aspect ratio label for given dims."""
    if height <= 0:
        return "1:1"
    target = width / height
    best_label, best_diff = "1:1", float("inf")
    for label, val in SUPPORTED_ASPECT_RATIOS:
        diff = abs(val - target)
        if diff < best_diff:
            best_label, best_diff = label, diff
    return best_label


def resize_to(image_bytes: bytes, target_wh: tuple[int, int]) -> bytes:
    """Resize Gemini output back to requested dimensions, output PNG.

    Always emits PNG to avoid lossy JPEG→PNG roundtrip when Gemini returns
    JPEG/WebP (sheet/character content has sharp strokes that compress poorly).
    """
    with Image.open(BytesIO(image_bytes)) as src:
        src.load()
        rgba = src.convert("RGBA") if src.mode != "RGBA" else src
        try:
            if rgba.size != target_wh:
                resized = rgba.resize(target_wh, Image.Resampling.LANCZOS)
            else:
                resized = rgba
            try:
                buf = BytesIO()
                resized.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
            finally:
                if resized is not rgba:
                    resized.close()
        finally:
            if rgba is not src:
                rgba.close()


def ensure_png_native(image_bytes: bytes) -> tuple[bytes, int, int]:
    """Normalize to PNG without resizing — returns (png_bytes, width, height).

    Used by the mix-swap core after rev5 dropped the forced resize to
    `sheet_geometry`. The downstream post-swap pipeline rescales geometry per
    actual output dim, so the caller needs to know the native (W_s, H_s).
    Always re-encodes (RGBA, optimize=True) so the upload step always sees PNG
    bytes regardless of the format Gemini returned.
    """
    with Image.open(BytesIO(image_bytes)) as src:
        src.load()
        rgba = src.convert("RGBA") if src.mode != "RGBA" else src
        try:
            w, h = rgba.size
            buf = BytesIO()
            rgba.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), w, h
        finally:
            if rgba is not src:
                rgba.close()


def build_dated_path(prefix: str) -> str:
    """`{prefix}/{YYYY-MM-DD}/{uuid_hex}-{ts_ms}.png` — no PII, just sortable."""
    ts = int(time.time() * 1000)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix}/{date}/{uuid.uuid4().hex}-{ts}.png"
