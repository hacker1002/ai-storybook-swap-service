"""Remove-bg adapter registry — public model id → adapter instance.

`get_remove_bg_adapter(model)` is called from `image_remove_bg_core` AFTER the
model has already cleared the per-job allowlist (`jobs.model_registry`
`RMBG_ALLOWLIST` / `resolve_model_params`). A miss here therefore means an
allowlisted model is MISSING its adapter — a config drift caught by the registry
parity unit test, not a client-facing path → raise `KeyError` (programmer error).

Adding model #N = +1 adapter file + 1 line in `_ADAPTERS` (Open/Closed); existing
adapters are untouched. Distinct name from `jobs.model_registry` (the job-param
allowlist) on purpose — this registry owns Replicate INPUT shape, not auth.
"""

from __future__ import annotations

from src.services.rmbg.base import RemoveBgAdapter
from src.services.rmbg.bria import BriaRemoveBgAdapter
from src.services.rmbg.labs_851 import Labs851RemoveBgAdapter

_ADAPTERS: dict[str, RemoveBgAdapter] = {
    BriaRemoveBgAdapter.model_id: BriaRemoveBgAdapter(),
    Labs851RemoveBgAdapter.model_id: Labs851RemoveBgAdapter(),
}


def get_remove_bg_adapter(model: str) -> RemoveBgAdapter:
    """Return the adapter for a public model id. Raises `KeyError` when missing
    (model passed the allowlist but no adapter ships — config drift)."""
    adapter = _ADAPTERS.get(model)
    if adapter is None:
        raise KeyError(f"no remove-bg adapter for model {model!r}")
    return adapter
