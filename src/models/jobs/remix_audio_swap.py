"""Pydantic models for `POST /api/jobs/remix/{remix_id}/audio-swap`.

Ported from image-api `src/models/jobs/remix_audio_swap.py`. Base class swapped
`FlexibleModel` → `pydantic.BaseModel`; behaviour identical (the request model
pins its own `model_config`). Only the request model + the two module constants
the handler needs are carried (routes return plain dicts).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_CONCURRENT_TEXTBOXES_PER_SPREAD",
    "MAX_RESULT_ERRORS",
    "RemixAudioSwapEnqueueRequest",
]


# Decision-locked: parallel textbox cap inside one spread. Not param-exposed.
MAX_CONCURRENT_TEXTBOXES_PER_SPREAD: int = 2

# Cap on result.errors[] to keep job row < 10 KB (lib contract).
MAX_RESULT_ERRORS: int = 100


# ─── Request ───────────────────────────────────────────────────────────────


class RemixAudioSwapEnqueueRequest(BaseModel):
    """Body for POST /api/jobs/remix/{remix_id}/audio-swap."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
        validate_default=True,
    )

    triggered_by: Literal["auto-create", "user"] = Field(
        alias="triggeredBy",
        description="Audit field — `auto-create` from frontend on createRemix, `user` on manual retry.",
    )
    max_concurrent_chunks_per_textbox: int = Field(
        default=4,
        ge=1,
        le=8,
        alias="maxConcurrentChunksPerTextbox",
        description="Cap on parallel narrate-script calls within a single textbox.",
    )
