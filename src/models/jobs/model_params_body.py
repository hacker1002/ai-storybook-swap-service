"""Shared `model_params` request leaf for the remix job enqueue bodies.

One typed nested shape `{model, params?}` imported by the swap job enqueue models
(sprite-swap 02, mix-swap 05) — DRY. Ported verbatim from image-api
`src/models/jobs/model_params_body.py`.

Security boundary: `model` is the PUBLIC allowlist id (a KEY into
`src.jobs.model_registry`), NEVER forwarded raw to a provider. `params` is an
open bag — the per-group adapter in the registry validates/clamps/drops keys, so
the leaf itself only forbids stray TOP-LEVEL keys (`{model, params}`), leaving
`params` free for forward-compat knobs.

A malformed shape (e.g. `model` not a string, or an extra top-level key) is a
Pydantic body error → HTTP 400 VALIDATION_ERROR (global handler). An unsupported
`model` value is a domain error raised later by `resolve_model_params` → HTTP 422
UNSUPPORTED_MODEL.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelParamsBody"]


class ModelParamsBody(BaseModel):
    """Optional per-job model selection: `{model: str, params?: {...}}`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str = Field(min_length=1)
    params: Optional[dict[str, Any]] = None
