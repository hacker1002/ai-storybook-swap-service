"""Per-model upscale INPUT adapter package.

Each model (real-esrgan, alexgenovese, recraft-crisp) owns its Replicate `input`
payload + capability flags (scale / face_enhance / tile cap / fixed-ratio) via an
`UpscaleAdapter`. Dispatch/retry/output stays in `services.image.upscale_core`.
"""

from src.services.upscale.base import UpscaleAdapter
from src.services.upscale.registry import get_upscale_adapter

__all__ = ["UpscaleAdapter", "get_upscale_adapter"]
