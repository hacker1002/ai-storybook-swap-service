"""Reusable AI cost-downscale for images sent inline to a vision model.

ONE place for the "shrink an image to cut AI cost BEFORE the call" policy, so the
detect-swap-defects core (06) and any future caller (swap 03/04) share the same
LANCZOS-resize-by-longest-edge + re-encode logic instead of re-deriving it.

Two SEPARATE concerns, do not conflate:
  - THIS util = the DEFAULT cost-resize by longest edge, run FIRST (cap resolution
    so the model isn't paid for pixels it can't use). `max_edge` is per-caller.
  - `gemini_payload_budget` = the HARD byte-cap that protects the 20MB inline
    limit, run AFTER (a guard, not a default). It JPEG-strips alpha; this util can
    keep PNG/alpha sharp for flat regions (gutter / cell strokes / ordinal badges)
    that matter for pixel-aligned comparison.

Contract:
  - Pure CPU (Pillow LANCZOS + re-encode). NO SSRF / NO network — the input is
    already bytes or a decoded `Image`. The caller wraps the call in
    `asyncio.to_thread`.
  - NEVER upscales. With `reencode='keep'` + an already-small image + bytes input,
    it is idempotent (returns the input bytes verbatim).
  - Two images with the SAME source dims + SAME `max_edge` produce the SAME output
    dims — the precondition the detect core relies on to keep the ORIGINAL and the
    recomposed RESULT sheet pixel-aligned.

Decompression-bomb safety is inherited from `Image.MAX_IMAGE_PIXELS` (set in
`image_ops`, imported for the side effect) — this util only handles already
decoded / already-bytes images.

PII: never logs bytes / base64.
"""

from __future__ import annotations

from io import BytesIO
from typing import Literal, Union

from PIL import Image

# Side-effect import: sets `Image.MAX_IMAGE_PIXELS` + `LOAD_TRUNCATED_IMAGES`
# (decompression-bomb guard) for any bytes we decode here.
from src.services import image_ops  # noqa: F401

__all__ = [
    "SHEET_AI_MAX_EDGE",
    "HUMAN_AI_MAX_EDGE",
    "VARIANT_AI_MAX_EDGE",
    "downscale_for_ai_cost",
]

# Default longest-edge caps (BALANCED policy — live in the util to stay DRY across
# callers; callers may still pass any `max_edge`).
SHEET_AI_MAX_EDGE = 2048  # px — longest edge of each sprite sheet (ORIGINAL + RESULT) for Gemini detect
HUMAN_AI_MAX_EDGE = 1024  # px — longest edge of each human reference image
# Mix-detect (07) variant sheets (old/new appearance tables): higher than
# HUMAN_AI_MAX_EDGE (1024) because each variant sheet GROUPS N target cells (each
# ~768px from swap 04) — need per-cell detail so Gemini can tell two targets
# apart (cross-contamination recall). Tune by measured precision/recall (spec 07
# OQ4 — `VARIANT_AI_MAX_EDGE` is a cheap constant to retune, never blocking).
VARIANT_AI_MAX_EDGE = 1536  # px — longest edge of each variant sheet (old / new)

_PNG_COMPRESS_LEVEL = 6
# Formats Pillow can round-trip cleanly for `reencode='keep'`; anything else
# (or a format-less in-memory Image) falls back to PNG.
_KEEP_PASSTHROUGH_FORMATS = {"PNG", "JPEG", "WEBP"}


def _encode(im: Image.Image, fmt: str, jpeg_quality: int) -> bytes:
    """Encode `im` to `fmt` ('PNG' | 'JPEG' | 'WEBP'). JPEG drops alpha (RGB)."""
    buf = BytesIO()
    if fmt == "JPEG":
        rgb = im.convert("RGB") if im.mode != "RGB" else im
        try:
            rgb.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        finally:
            if rgb is not im:
                rgb.close()
    elif fmt == "WEBP":
        im.save(buf, format="WEBP", quality=jpeg_quality)
    else:  # PNG (default)
        im.save(buf, format="PNG", optimize=True, compress_level=_PNG_COMPRESS_LEVEL)
    return buf.getvalue()


def downscale_for_ai_cost(
    img: Union["Image.Image", bytes],
    *,
    max_edge: int,
    reencode: Literal["png", "jpeg", "keep"] = "png",
    jpeg_quality: int = 85,
) -> tuple[bytes, int, int]:
    """Resize so `max(w, h) <= max_edge` (LANCZOS, NEVER upscale) + re-encode.

    Returns `(out_bytes, out_w, out_h)`. `reencode`:
      - 'png'  → always PNG (keeps alpha + flat regions crisp);
      - 'jpeg' → always JPEG q=`jpeg_quality` (smaller; drops alpha);
      - 'keep' → re-encode to the source format when resized; when NOT resized and
                 the input is bytes, return the input bytes verbatim (idempotent).

    Pure / sync — caller wraps in `asyncio.to_thread`. No SSRF/fetch.
    """
    if max_edge <= 0:
        raise ValueError(f"max_edge must be positive, got {max_edge}")
    if reencode not in ("png", "jpeg", "keep"):
        raise ValueError(f"invalid reencode mode: {reencode!r}")

    src_bytes = bytes(img) if isinstance(img, (bytes, bytearray)) else None
    owns_decoded = src_bytes is not None

    if owns_decoded:
        # `Image.open` is lazy; the actual decode happens in `.load()` INSIDE the
        # try → a corrupt/truncated input raising there still hits `finally.close()`
        # (no leaked Image handle).
        decoded = Image.open(BytesIO(src_bytes))
    else:
        decoded = img  # caller-owned PIL Image — do NOT close it

    work: Image.Image = decoded
    resized = False
    try:
        if owns_decoded:
            decoded.load()  # force decode (decompression-bomb guard applies)
        src_format = (getattr(decoded, "format", None) or "").upper()
        w, h = decoded.size
        longest = max(w, h)
        if longest > max_edge:
            scale = max_edge / longest
            out_w = max(1, round(w * scale))
            out_h = max(1, round(h * scale))
            work = decoded.resize((out_w, out_h), Image.Resampling.LANCZOS)
            resized = True
        else:
            out_w, out_h = w, h

        # Idempotent fast-path: keep + no resize + bytes input → return raw bytes.
        if reencode == "keep" and not resized and src_bytes is not None:
            return src_bytes, out_w, out_h

        if reencode == "png":
            fmt = "PNG"
        elif reencode == "jpeg":
            fmt = "JPEG"
        else:  # keep
            fmt = src_format if src_format in _KEEP_PASSTHROUGH_FORMATS else "PNG"

        return _encode(work, fmt, jpeg_quality), out_w, out_h
    finally:
        if resized and work is not decoded:
            work.close()
        if owns_decoded:
            decoded.close()
