"""Upscale adapter registry — public model id → adapter instance.

`get_upscale_adapter(model)` is called from `upscale_core.run_upscale` AFTER the
model has already cleared the per-job allowlist (`jobs.model_registry`
`UPSCALE_ALLOWLIST` / `resolve_model_params`). A miss here therefore means an
allowlisted model is MISSING its adapter — a config drift caught by the registry
parity unit test, not a client-facing path → raise `KeyError` (programmer error).

Adding model #N = +1 adapter file + 1 line in `_ADAPTERS` (Open/Closed); existing
adapters are untouched. Distinct from `jobs.model_registry` (the job-param
allowlist) on purpose — this registry owns Replicate INPUT shape + capabilities,
not auth. Mirrors `services/rmbg/registry.py`.
"""

from __future__ import annotations

from src.services.upscale.alexgenovese import AlexgenoveseUpscalerAdapter
from src.services.upscale.base import UpscaleAdapter
from src.services.upscale.real_esrgan import RealEsrganAdapter
from src.services.upscale.recraft_crisp import RecraftCrispUpscaleAdapter
from src.services.upscale.xinntao_realesrgan import XinntaoRealesrganAdapter

_ADAPTERS: dict[str, UpscaleAdapter] = {
    RealEsrganAdapter.model_id: RealEsrganAdapter(),
    AlexgenoveseUpscalerAdapter.model_id: AlexgenoveseUpscalerAdapter(),
    RecraftCrispUpscaleAdapter.model_id: RecraftCrispUpscaleAdapter(),
    XinntaoRealesrganAdapter.model_id: XinntaoRealesrganAdapter(),
}


def get_upscale_adapter(model: str) -> UpscaleAdapter:
    """Return the adapter for a public model id. Raises `KeyError` when missing
    (model passed the allowlist but no adapter ships — config drift)."""
    adapter = _ADAPTERS.get(model)
    if adapter is None:
        raise KeyError(f"no upscale adapter for model {model!r}")
    return adapter
