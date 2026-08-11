"""Per-model upscale INPUT adapter (payload + per-model capability flags).

Each Replicate upscaler differs along more axes than remove-bg (which only varies
the input payload): some support a `scale` knob and tiling, some restore faces,
some are fixed-ratio "native passthrough" crisp sharpeners with no scale at all.
An adapter owns the ONLY parts that differ per model:

  - `build_payload` — the Replicate `input` dict.
  - `version` — dispatch mode (mirrors `RemoveBgAdapter`): None → OFFICIAL
    (`model=owner/name`), set → COMMUNITY pinned `version=<hash>`. ⚡Validation S1
    pins ALL v1 upscale adapters to a `version=` (recraft included) so dispatch is
    deterministic; the `model=` branch in the core is kept forward-compat only.
  - `supports_scale` — False → core omits scale from the payload AND skips any
    post-resize: the model returns its own fixed-ratio output (native passthrough).
  - `supports_face_enhance` — False → core never sets `face_enhance`.
  - `max_input_pixels` — drives the core's auto-tile gate. None → NEVER tile
    (hosted/fixed-ratio model with no GPU pixel cap to respect).

Dispatch (slot / 429-retry / output extraction) stays in `upscale_core`, which
calls `build_payload` then dispatches by version|model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.models.requests.upscale_image import REAL_ESRGAN_MAX_INPUT_PIXELS


class UpscaleAdapter(ABC):
    """Base class for per-model Replicate upscale input adapters.

    Subclasses set `model_id` (public owner/name == allowlist key) + the four
    capability flags, and implement `build_payload`. The registry keys adapters
    by `model_id`.
    """

    model_id: str = ""
    version: str | None = None  # None → official model= ; set → community version=
    supports_scale: bool = True  # False → no scale payload + no post-resize (native)
    supports_face_enhance: bool = True  # False → no face_enhance payload
    max_input_pixels: int | None = REAL_ESRGAN_MAX_INPUT_PIXELS  # None → never tile

    @abstractmethod
    def build_payload(
        self, image_value: str, scale: float, face_enhance: bool
    ) -> dict:
        """Return the Replicate `input` dict for this model.

        `image_value` is an HTTPS URL or a data URI (both accepted by a
        `format: uri` field). `scale` / `face_enhance` are the public knobs —
        adapters whose model lacks a knob ignore the corresponding argument
        (e.g. recraft ignores both).
        """
        raise NotImplementedError
