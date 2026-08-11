"""xinntao `xinntao/realesrgan` adapter — the NEW DEFAULT upscaler (Anime variant).

Real-ESRGAN family upscaler with selectable weight variants. v1 hardcodes the
`Anime - anime6B` variant (best for the stylized storybook art this service
upscales). Two load-bearing divergences vs the other upscale adapters, both
isolated HERE so the core + the other 3 adapters stay byte-identical:

  1. Input image key is `img` (every other adapter uses `image`).
  2. A `version` INPUT field selects the weight variant (the enum, e.g.
     "Anime - anime6B") — NOT to be confused with the Replicate `version=`
     DISPATCH pin (the class attr `version`, the hash).

COMMUNITY model → pinned `version=` (the `model=owner/name` endpoint 404s for
community models — see real-esrgan-vs-community dispatch). `face_enhance`
(GFPGAN) is a NO-OP on the Anime variant per the model card, but
`supports_face_enhance=True` is kept for forward-compat with the General variant
and to keep the public knob present (it is OMITTED-WHEN-FALSE for byte-parity
with the real-esrgan idiom). Reuses the conservative 2,096,704 px tile cap.
"""

from __future__ import annotations

from src.models.requests.upscale_image import (
    REAL_ESRGAN_MAX_INPUT_PIXELS,
    XINNTAO_REALESRGAN_DEFAULT_VARIANT,
    XINNTAO_REALESRGAN_VERSION,
)
from src.services.upscale.base import UpscaleAdapter


class XinntaoRealesrganAdapter(UpscaleAdapter):
    model_id = "xinntao/realesrgan"
    version = XINNTAO_REALESRGAN_VERSION  # COMMUNITY → pinned version= dispatch
    supports_scale = True
    supports_face_enhance = True  # GFPGAN no-op on anime; kept for General variant
    max_input_pixels = REAL_ESRGAN_MAX_INPUT_PIXELS  # conservative reuse
    # Weight-variant selected via the `version` INPUT field (NOT the dispatch pin).
    # Instance attr → forward-compat override seam (not exposed publicly v1).
    variant = XINNTAO_REALESRGAN_DEFAULT_VARIANT

    def build_payload(
        self, image_value: str, scale: float, face_enhance: bool
    ) -> dict:
        # Divergence 1: key `img` (not `image`). Divergence 2: `version` input
        # field = weight variant. face_enhance omit-when-false (parity real-esrgan).
        payload: dict = {"img": image_value, "scale": scale, "version": self.variant}
        if face_enhance:
            payload["face_enhance"] = True
        return payload
