"""recraft `recraft-ai/recraft-crisp-upscale` adapter — fixed-ratio crisp sharpen.

NOT a scaler: it sharpens/de-blurs at a FIXED output ratio (no `scale` input, no
face restore). The core treats it as a NATIVE PASSTHROUGH — no scale in the
payload, no post-resize, output = the model's own dims, `meta.fixedRatio=true`,
`scale` echoed only. `max_input_pixels=None` → the core NEVER tiles it (no GPU
pixel cap to respect; Issue A — the tile/INPUT_TOO_LARGE gate is skipped entirely).

DISPATCH (⚡Validation S1): pinned `version=` (NOT `model=`). The versions-list 404
cannot distinguish official-vs-community, and `version=` dispatches for BOTH → it
removes the only live-test dispatch unknown. Tradeoff: no auto-update — bump
`RECRAFT_CRISP_UPSCALE_VERSION` manually on a new release.
"""

from __future__ import annotations

from src.models.requests.upscale_image import RECRAFT_CRISP_UPSCALE_VERSION
from src.services.upscale.base import UpscaleAdapter


class RecraftCrispUpscaleAdapter(UpscaleAdapter):
    model_id = "recraft-ai/recraft-crisp-upscale"
    version = RECRAFT_CRISP_UPSCALE_VERSION  # ⚡S1: pinned, NOT None
    supports_scale = False  # fixed-ratio → native passthrough (no scale, no resize)
    supports_face_enhance = False
    max_input_pixels = None  # no GPU pixel cap → never tile, never px-reject

    def build_payload(
        self, image_value: str, scale: float, face_enhance: bool
    ) -> dict:
        del scale, face_enhance  # recraft has neither input
        return {"image": image_value}
