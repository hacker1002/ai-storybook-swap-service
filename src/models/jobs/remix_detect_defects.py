"""Pydantic models for `POST /api/jobs/remix/{remix_id}/detect-sprite-defects`.

Enqueue model for the swap defect-detection job (`remix_detect_defects`) — a 1:1
MIRROR of the sprite-swap enqueue (`remix_sprite_swap.py`) that orchestrates the
AI core `run_detect_swap_defects()` over every SWAPPED crop sheet of ONE sprite.

Divergences vs the sprite-swap enqueue model:
  - NO `model_params` — the detect core hardcodes a factual temperature (0.1) and
    runs gemini-3.5-flash; `swap_model` / `swap_temperature` are DISPLAY-only
    context the core renders into `builder_params` (NEVER used to call Gemini);
  - adds optional detection controls (`focus_objects` / `severity_threshold` /
    `max_defects`) bounded 1..80;
  - the no-swap precondition is `422 NO_SWAP_RESULT` (NOT a `200 skipped`,
    Validation S1) — surfaced by the router, not this model.

Defects are ADVISORY / ephemeral: the handler writes them to
`background_jobs.result.defectsBySheet`, NEVER to `remixes`.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field

from src.models.base import FlexibleModel

__all__ = [
    "MAX_RESULT_ERRORS",
    "DETECT_MAX_DEFECTS_CAP",
    "RemixDetectDefectsEnqueueRequest",
]


# Cap on result.errors[] to keep the job row small (lib contract — mirror swap).
MAX_RESULT_ERRORS: int = 100

# Per-sheet defect cap ceiling (core default 30, hard cap 80 — parity with
# `DetectSwapDefectsRequest.max_defects`).
DETECT_MAX_DEFECTS_CAP: int = 80


# ─── Request ───────────────────────────────────────────────────────────────


class RemixDetectDefectsEnqueueRequest(FlexibleModel):
    """Body for POST /api/jobs/remix/{remix_id}/detect-sprite-defects.

    `extra="forbid"` rejects unknown fields. The detect controls are all optional
    — omit → core defaults (severity 'low', max_defects 30). `max_defects` is
    bounded 1..80 at the schema layer (out-of-range → Pydantic body error → the
    global handler emits 400 VALIDATION_ERROR).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )

    sprite_id: str = Field(
        min_length=1,
        description="sprite id (uuid) = sprites[].id. Scope = every SWAPPED crop sheet of this sprite.",
    )
    force_resweep: bool = Field(
        default=True,
        description="carried for contract symmetry with sprite-swap; detect does not persist, so it never gates scope (every swapped sheet is always inspected).",
    )
    # ── builder context (display-only — NOT used to call Gemini) ──
    swap_model: Optional[str] = Field(
        default=None,
        max_length=120,
        description="model used by the swap being inspected; rendered as context into the detect builder_params.",
    )
    swap_temperature: Optional[float] = Field(
        default=None,
        description="temperature used by the swap being inspected; context only.",
    )
    # ── detect controls (forwarded per-sheet to the core) ──
    focus_objects: Optional[list[str]] = Field(
        default=None,
        description="restrict reported defects to these object_keys (must be a subset of the sprite lineup).",
    )
    severity_threshold: Optional[Literal["low", "medium", "high"]] = Field(
        default=None,
        description="drop defects below this severity; omit → core default 'low'.",
    )
    max_defects: Optional[int] = Field(
        default=None,
        ge=1,
        le=DETECT_MAX_DEFECTS_CAP,
        description="per-sheet cap on reported defects; omit → core default 30.",
    )
