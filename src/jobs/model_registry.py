"""Per-job model allowlist registry for the remix job pipeline.

Single source of truth mapping a PUBLIC model id → an adapter, per job group
(`swap`, `rmbg`, `upscale`). Pure logic, no I/O — fast and deterministic to test.

Security boundary (the core reason this module exists): the client-supplied
`model` is a KEY into a per-group allowlist, NEVER forwarded raw to a provider.
This blocks arbitrary-model cost/abuse + injection. Numeric knobs are clamped to
safe ranges. Unknown param keys are dropped (forward-compat).

`resolve_model_params(model_params, group)` runs at enqueue Step 1 (after auth +
body validation, BEFORE `create_task`) so it can raise `UNSUPPORTED_MODEL` (422)
early. It returns a normalized dict that is BOTH persisted into
`background_jobs.params.model_params` AND forwarded into the core request inside
the handler.

Design decision D1 (provider-agnostic registry): the registry validates the
PUBLIC id + clamps params only. It does NOT map public→provider ids and does NOT
emit a `provider_model` key. The normalized shape is uniform `{model, params}`
across all groups, `model` always a public id. swap cores own the public→Gemini
map internally (see `swap_*_sheet_core.py::_PUBLIC_TO_GEMINI`); rmbg/upscale use
owner/name refs (public == provider) so the public id forwards straight to
Replicate at the core.

`model_params is None` (omitted at enqueue) → the resolved DEFAULT dict is
returned and persisted (D2: every post-ship job row records the model actually
used — audit/replay/LangSmith parity).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.models.gemini_image_models import PUBLIC_NANO_BANANA_PRO
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)

__all__ = [
    "NOT_SUPPORTED",
    "SWAP_ALLOWLIST",
    "SWAP_DEFAULT_MODEL",
    "SWAP_DEFAULT_TEMPERATURE",
    "SWAP_TEMPERATURE_RANGE",
    "RMBG_ALLOWLIST",
    "RMBG_DEFAULT_MODEL",
    "TEXT_REMOVAL_ALLOWLIST",
    "TEXT_REMOVAL_DEFAULT_MODEL",
    "UPSCALE_ALLOWLIST",
    "UPSCALE_DEFAULT_MODEL",
    "UPSCALE_NOISE_RANGE",
    "UPSCALE_ALLOWED_KEYS",
    "UPSCALE_DEFAULT_FACE_ENHANCE",
    "resolve_model_params",
]


# --- Sentinels & group allowlists (mapping constants — keep verbatim) ------

# Sentinel for a model that is registered (recognized public id) but whose
# adapter is not shipped yet → resolve raises UNSUPPORTED_MODEL (422). Lets the
# allowlist document intended-but-deferred models without dispatching them.
NOT_SUPPORTED = "NOT_SUPPORTED"


# group "swap" — shared by job 02 (sprite-swap) + job 05 (mix-swap).
SWAP_ALLOWLIST: dict[str, str] = {
    PUBLIC_NANO_BANANA_PRO: "gemini",        # DEFAULT (public id from shared const)
    "openai/gpt-image-2": NOT_SUPPORTED,     # → 422
    "bytedance/seedream-4.5": NOT_SUPPORTED,  # → 422
}
SWAP_DEFAULT_MODEL = PUBLIC_NANO_BANANA_PRO
# OQ#1 resolved: single default temperature for BOTH swap jobs (sprite + mix) =
# 0.25 (no per-job split, no default_overrides). Core constant GEMINI_TEMPERATURE
# is already 0.25 on both cores → NO behavior change. D1: registry is
# provider-agnostic — the public→Gemini id map lives in the swap cores, not here.
SWAP_DEFAULT_TEMPERATURE = 0.25
SWAP_TEMPERATURE_RANGE: tuple[float, float] = (0.0, 2.0)


# group "rmbg" — job 09. No numeric params in v1; adapter only maps the model.
RMBG_ALLOWLIST: dict[str, str] = {
    "bria/remove-background": "replicate",       # DEFAULT
    "851-labs/background-remover": "replicate",
}
RMBG_DEFAULT_MODEL = "bria/remove-background"


# group "text-removal" — sync `/api/retouch/remove-text-image`. Single OFFICIAL
# model, no numeric params in v1; adapter only validates + maps the model.
TEXT_REMOVAL_ALLOWLIST: dict[str, str] = {
    "flux-kontext-apps/text-removal": "replicate",  # DEFAULT
}
TEXT_REMOVAL_DEFAULT_MODEL = "flux-kontext-apps/text-removal"


# group "upscale" — job 10. ⚡2026-06-23 multi-model: recraft + alexgenovese FLIPPED
# NOT_SUPPORTED → replicate (per-model adapter shipped in services/upscale; all 3
# pin version= → deterministic dispatch, Validation S1).
UPSCALE_ALLOWLIST: dict[str, str] = {
    "xinntao/realesrgan": "replicate",               # ⚡2026-06-29 NEW DEFAULT (Anime)
    "nightmareai/real-esrgan": "replicate",
    "recraft-ai/recraft-crisp-upscale": "replicate",  # fixed-ratio crisp
    "alexgenovese/upscaler": "replicate",            # Real-ESRGAN+GFPGAN clone
}
# ⚡2026-06-29 (design 0065b92): DEFAULT flipped nightmareai/real-esrgan →
# xinntao/realesrgan (Anime). Single constant → applies to EVERY omit surface
# (sync `/api/image/upscale-image` omit-path + job 10 enqueue omit-path, both via
# resolve_model_params, and the core's `req.model or UPSCALE_DEFAULT_MODEL`).
# Behavioral: face_enhance becomes a no-op on the anime default (GFPGAN skipped) —
# users needing real face-restore must explicitly pick real-esrgan/alexgenovese.
UPSCALE_DEFAULT_MODEL = "xinntao/realesrgan"
UPSCALE_NOISE_RANGE: tuple[float, float] = (0.0, 10.0)
# Per-model allowed param keys. `_normalize_upscale` filters to the model's set.
#  - real-esrgan: {face_enhance, noise}. `noise` kept for forward-compat (clamped
#    [0,10]) but DROPPED at dispatch — real-esrgan adapter has no `noise` input.
#  - alexgenovese: {face_enhance} only (Real-ESRGAN+GFPGAN clone, no noise input).
#  - recraft: set() — fixed-ratio crisp upscaler, no numeric knob (no scale/face).
#  - xinntao: {face_enhance} only. GFPGAN no-op on the Anime variant (kept for the
#    public knob + forward-compat General variant); no `noise` input.
UPSCALE_ALLOWED_KEYS: dict[str, set[str]] = {
    "xinntao/realesrgan": {"face_enhance"},
    "nightmareai/real-esrgan": {"face_enhance", "noise"},
    "alexgenovese/upscaler": {"face_enhance"},
    "recraft-ai/recraft-crisp-upscale": set(),
}
UPSCALE_DEFAULT_FACE_ENHANCE = True  # real-esrgan GFPGAN default


# --- Adapters (pure normalize functions) ----------------------------------


def _clamp(value: float, lo: float, hi: float, *, key: str, group: str) -> float:
    """Clamp a numeric to [lo, hi]; log a warn (key + old→new only) when it moves.

    PII discipline: log key + numeric values only, never the full params object.
    """
    clamped = min(max(value, lo), hi)
    if clamped != value:
        logger.warning(
            "model_registry_clamp group=%s key=%s old=%s new=%s",
            group, key, value, clamped,
        )
    return clamped


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort numeric coercion; None when not a real number.

    Pydantic body validation already guarantees JSON-typed values, but the
    registry is also reachable from handler-side resolution, so a bool slips in
    as 0/1 unintentionally — reject bool explicitly (it is an int subclass).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _normalize_swap(params: dict[str, Any]) -> dict[str, Any]:
    """swap group: clamp `temperature` to [0,2] (default 0.25 when absent),
    drop every other key. Provider-agnostic — no public→provider mapping here
    (the swap cores resolve the Gemini id from the public id internally).
    """
    raw = _coerce_float(params.get("temperature"))
    temperature = SWAP_DEFAULT_TEMPERATURE if raw is None else _clamp(
        raw, *SWAP_TEMPERATURE_RANGE, key="temperature", group="swap"
    )
    return {"temperature": temperature}


def _normalize_rmbg(params: dict[str, Any]) -> dict[str, Any]:
    """rmbg group: no params in v1 — drop everything (model-only selection)."""
    del params  # explicit: rmbg honors no knobs v1
    return {}


def _normalize_text_removal(params: dict[str, Any]) -> dict[str, Any]:
    """text-removal group: no params in v1 — drop everything (model-only)."""
    del params  # explicit: text-removal honors no knobs v1
    return {}


def _normalize_upscale(model: str, params: dict[str, Any]) -> dict[str, Any]:
    """upscale group (real-esrgan / alexgenovese / recraft — ⚡2026-06-23 multi-model).
    Filter to the model's allowed keys; clamp `noise` to [0,10] (real-esrgan
    forward-compat — kept in params but DROPPED at dispatch, adapter has no noise
    input); coerce `face_enhance` to bool (default True); drop keys outside the set
    (recraft set() → all dropped; alexgenovese {face_enhance} → noise dropped).
    """
    allowed = UPSCALE_ALLOWED_KEYS.get(model, set())
    out: dict[str, Any] = {}
    if "face_enhance" in allowed:
        out["face_enhance"] = bool(params.get("face_enhance", UPSCALE_DEFAULT_FACE_ENHANCE))
    if "noise" in allowed:
        raw = _coerce_float(params.get("noise"))
        if raw is not None:
            out["noise"] = _clamp(
                raw, *UPSCALE_NOISE_RANGE, key="noise", group="upscale"
            )
    return out


# Group dispatch table: group → (allowlist, default_model).
_GROUPS: dict[str, tuple[dict[str, str], str]] = {
    "swap": (SWAP_ALLOWLIST, SWAP_DEFAULT_MODEL),
    "rmbg": (RMBG_ALLOWLIST, RMBG_DEFAULT_MODEL),
    "text-removal": (TEXT_REMOVAL_ALLOWLIST, TEXT_REMOVAL_DEFAULT_MODEL),
    "upscale": (UPSCALE_ALLOWLIST, UPSCALE_DEFAULT_MODEL),
}


def _raise_unsupported(model: str, group: str) -> None:
    logger.warning("model_registry_unsupported group=%s model=%s", group, model)
    raise RemixDomainError(
        status=422,
        code="UNSUPPORTED_MODEL",
        message=f"model '{model}' is not supported for group '{group}'",
        details={"model": model},
    )


def resolve_model_params(
    model_params: Optional[dict[str, Any]], group: str
) -> dict[str, Any]:
    """Resolve + normalize a per-job model selection into a persistable dict.

    Returns the uniform normalized shape `{model, params}` (public id only, D1):
      - `model_params is None` → the group default model + default params (no raise).
      - `model` not in the group allowlist → `UNSUPPORTED_MODEL` (422).
      - `model` allowlisted but adapter == NOT_SUPPORTED → `UNSUPPORTED_MODEL` (422).
      - otherwise → `{model, params}` with params clamped + unknown keys dropped.

    Raises:
      RemixDomainError(422, UNSUPPORTED_MODEL, details={"model": ...}) — bad model.
      ValueError — unknown `group` (programmer error; not a client-facing path).
    """
    if group not in _GROUPS:
        raise ValueError(f"unknown model group: {group!r}")
    allowlist, default_model = _GROUPS[group]

    if model_params is None:
        model = default_model
        params_in: dict[str, Any] = {}
    else:
        model = model_params["model"]
        params_in = model_params.get("params") or {}

    adapter = allowlist.get(model)
    if adapter is None or adapter == NOT_SUPPORTED:
        _raise_unsupported(model, group)

    if group == "swap":
        params_out = _normalize_swap(params_in)
    elif group == "rmbg":
        params_out = _normalize_rmbg(params_in)
    elif group == "text-removal":
        params_out = _normalize_text_removal(params_in)
    else:  # upscale
        params_out = _normalize_upscale(model, params_in)

    logger.debug(
        "model_registry_resolved group=%s model=%s param_keys=%s",
        group, model, sorted(params_out.keys()),
    )
    return {"model": model, "params": params_out}
