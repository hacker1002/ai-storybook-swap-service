"""alexgenovese `alexgenovese/upscaler` adapter — Real-ESRGAN+GFPGAN clone.

Schema-identical drop-in for real-esrgan (image / scale / face_enhance). COMMUNITY
model → pinned `version=` (the `model=owner/name` endpoint 404s for community
models). Reuses the conservative 2,096,704 px tile cap (Validation S1 — tune the
per-adapter `max_input_pixels` by 1 line if a live OOM proves the GPU cap differs).

Issue B — face_enhance default DIVERGES from real-esrgan: alexgenovese defaults
face_enhance to TRUE upstream, so omit-when-false (real-esrgan's idiom) cannot turn
it OFF. This adapter therefore sets `face_enhance` ALWAYS-EXPLICITLY (both true and
false), so the public knob actually controls it.
"""

from __future__ import annotations

from src.models.requests.upscale_image import (
    ALEXGENOVESE_UPSCALER_VERSION,
    REAL_ESRGAN_MAX_INPUT_PIXELS,
)
from src.services.upscale.base import UpscaleAdapter


class AlexgenoveseUpscalerAdapter(UpscaleAdapter):
    model_id = "alexgenovese/upscaler"
    version = ALEXGENOVESE_UPSCALER_VERSION
    supports_scale = True
    supports_face_enhance = True
    max_input_pixels = REAL_ESRGAN_MAX_INPUT_PIXELS  # conservative reuse (S1)

    def build_payload(
        self, image_value: str, scale: float, face_enhance: bool
    ) -> dict:
        # ALWAYS-EXPLICIT (issue B): default-true upstream → must set both values.
        return {
            "image": image_value,
            "scale": scale,
            "face_enhance": bool(face_enhance),
        }
