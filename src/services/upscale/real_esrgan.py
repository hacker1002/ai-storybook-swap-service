"""Real-ESRGAN `nightmareai/real-esrgan` adapter — the v1 default upscaler.

GAN upscaler with optional GFPGAN face restoration. Native `scale` + auto-tile
(GPU cap 2,096,704 px). `face_enhance` is OMITTED-WHEN-FALSE (the historical
idiom) to keep byte-parity with the pre-adapter payload — see Issue B: real-esrgan
defaults face_enhance to false upstream, so omitting it == off.
"""

from __future__ import annotations

from src.models.requests.upscale_image import (
    REAL_ESRGAN_MAX_INPUT_PIXELS,
    REAL_ESRGAN_VERSION,
)
from src.services.upscale.base import UpscaleAdapter


class RealEsrganAdapter(UpscaleAdapter):
    model_id = "nightmareai/real-esrgan"
    version = REAL_ESRGAN_VERSION
    supports_scale = True
    supports_face_enhance = True
    max_input_pixels = REAL_ESRGAN_MAX_INPUT_PIXELS

    def build_payload(
        self, image_value: str, scale: float, face_enhance: bool
    ) -> dict:
        payload: dict = {"image": image_value, "scale": scale}
        if face_enhance:  # omit-when-false → byte-parity with legacy payload
            payload["face_enhance"] = True
        return payload
