"""Job handler registrations (side-effect imports).

Importing this package registers every handler with the runner's `_REGISTRY`
(via each module's `@register(...)` decorator at import time). `main.py` imports
this package ABOVE `app = FastAPI(...)` so the registry is populated before any
enqueue can run — an empty registry makes `enqueue` raise at runtime.

P3b ships the 8 remix job handlers + the demo handler (used to smoke-test the lib
end-to-end without any AI call).
"""

from src.jobs.handlers import demo_long_running  # noqa: F401 — side-effect: @register
from src.jobs.handlers import (  # noqa: F401 — side-effect: @register (stage + detect)
    remix_detect_defects,
    remix_detect_mix_defects,
    remix_detect_rmbg_defects,
    remix_rmbg,
    remix_upscale,
)
from src.jobs.handlers import (  # noqa: F401 — side-effect: @register (swap group)
    remix_audio_swap,
    remix_mix_swap,
    remix_sprite_swap,
)

__all__ = [
    "demo_long_running",
    "remix_rmbg",
    "remix_upscale",
    "remix_detect_defects",
    "remix_detect_mix_defects",
    "remix_detect_rmbg_defects",
    "remix_sprite_swap",
    "remix_audio_swap",
    "remix_mix_swap",
]
