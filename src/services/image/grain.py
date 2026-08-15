"""Monochrome watercolor-paper grain post-process (pure numpy + Pillow).

Ported VERBATIM from `ai-storybook-python-api/src/services/image/grain.py` (P3b).
Model-agnostic CPU transform applied AFTER upscale (on output bytes — never
forwarded to Replicate). Alpha-preserving (splits alpha, adds noise to RGB only,
recombines), PNG output. Callers MUST guard `GRAIN_MAX_PIXELS` + wrap in
`asyncio.to_thread`. Raises `ValueError` on undecodable bytes (caller treats grain
as non-fatal).
"""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageFilter, UnidentifiedImageError

from src.models.requests.upscale_image import (
    GRAIN_DEFAULT_AMP,
    GRAIN_DEFAULT_BLUR,
    GRAIN_DEFAULT_SEED,
)

__all__ = ["apply_watercolor_grain"]


def apply_watercolor_grain(
    png_bytes: bytes,
    *,
    amp: float = GRAIN_DEFAULT_AMP,
    blur: float = GRAIN_DEFAULT_BLUR,
    seed: int = GRAIN_DEFAULT_SEED,
) -> bytes:
    """Add monochrome watercolor grain; return PNG bytes (same WxH, same mode).

    RGBA inputs keep their original alpha channel byte-for-byte; only the RGB
    planes are perturbed. Raises `ValueError` on undecodable input bytes.
    """
    try:
        img = Image.open(BytesIO(png_bytes))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"grain: undecodable image bytes ({exc})") from exc

    # 1) split alpha (any mode carrying an alpha band) → add noise to RGB only.
    alpha = img.getchannel("A") if "A" in img.getbands() else None
    rgb = img.convert("RGB")
    a = np.asarray(rgb).astype(np.float32)  # (H, W, 3)
    h, w = a.shape[0], a.shape[1]

    # 2) Gaussian noise at image resolution → fine, sharp grain.
    rng = np.random.default_rng(seed)
    g = rng.standard_normal((h, w)).astype(np.float32)

    # 3) soften the grain (watercolor-paper feel).
    g_min, g_max = float(g.min()), float(g.max())
    denom = (g_max - g_min) or 1.0  # guard the degenerate constant-noise case
    gi = Image.fromarray(((g - g_min) / denom * 255).astype(np.uint8))
    gi = gi.filter(ImageFilter.GaussianBlur(blur))
    gg = np.asarray(gi).astype(np.float32)

    # 4) standardize to mean 0 / std 1, scale by amp, broadcast across 3 channels.
    gg = (gg - gg.mean()) / (gg.std() + 1e-6)
    noise = (gg * amp)[:, :, None]
    out = np.clip(a + noise, 0, 255).astype(np.uint8)

    out_img = Image.fromarray(out, mode="RGB")
    if alpha is not None:
        out_img.putalpha(alpha)  # → RGBA, original alpha intact

    buf = BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue()
