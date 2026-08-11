"""Pydantic models for `POST /api/jobs/remix/{remix_id}/sprite-swap`.

Ported from image-api `src/models/jobs/remix_sprite_swap.py`. Only the request
model + the two constants the handler/route need are carried (the documentary
response/JSONB models in image-api are omitted — routes return plain dicts). The
base is plain `pydantic.BaseModel` (image-api's `FlexibleModel` with
`extra="forbid"` is behaviourally just BaseModel + forbid).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.models.jobs.model_params_body import ModelParamsBody

__all__ = [
    "MAX_CONCURRENT_SHEETS",
    "MAX_RESULT_ERRORS",
    "RemixSpriteSwapEnqueueRequest",
]


# Gemini concurrency is gated inside `run_swap_sprite_sheet` (shared `_gemini_sem`
# cap=3 with sync endpoints). The job runs sheets SEQUENTIALLY (one full-column
# write after gather → single-writer) → pin to 1. Not param-exposed.
MAX_CONCURRENT_SHEETS: int = 1

# Cap on result.errors[] to keep job row < 10 KB (lib contract).
MAX_RESULT_ERRORS: int = 100


class RemixSpriteSwapEnqueueRequest(BaseModel):
    """Body for POST /api/jobs/remix/{remix_id}/sprite-swap.

    `extra="forbid"` rejects unknown fields. `model_params` (Phase 02 wiring) is a
    typed nested optional — omit → registry default (group `swap`, temp 0.25).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )

    sprite_id: str = Field(
        min_length=1,
        description="sprite id (uuid) = sprites[].id. Scope = every crop sheet of this sprite.",
    )
    force_resweep: bool = Field(
        default=False,
        description="true → clear + re-swap every sheet. false → idempotent skip of sheets with an is_selected swap.",
    )
    model_params: Optional[ModelParamsBody] = Field(
        default=None,
        description="optional model selection (group 'swap'); omit → default model + temp 0.25.",
    )
