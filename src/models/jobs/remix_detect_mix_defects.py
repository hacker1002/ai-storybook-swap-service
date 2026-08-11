"""Pydantic models for `POST /api/jobs/remix/{remix_id}/detect-mix-defects`.

Enqueue model for the MIX-plane swap defect-detection job
(`remix_detect_mix_defects`) — sibling of the sprite-plane detect enqueue
(`remix_detect_defects.py`, job 11) that orchestrates the AI core
`run_detect_mix_defects()` ([api/remix/07]) over every SWAPPED crop sheet of ONE
mix BATCH (`remixes.mixes[]`, identified by `id`).

Divergences vs the sprite-plane detect enqueue model (job 11):
  - scoped by `batch_id` (= `mixes[].id`), NOT `sprite_id` — the lineup is
    derived at runtime from the aggregate `original_crops[].tags[]`;
  - everything else mirrors job 11: NO `model_params` (detect hardcodes a factual
    temperature; `swap_model` / `swap_temperature` are DISPLAY-only context the
    core renders into `builder_params`), optional detection controls
    (`focus_objects` / `severity_threshold` / `max_defects` 1..80).

Defects are ADVISORY / ephemeral: the handler writes them to
`background_jobs.result.defectsBySheet`, NEVER to `remixes`. The 3 documentary
response models exist for reuse + doc parity; the router returns plain dicts.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field

from src.models.base import FlexibleModel

__all__ = [
    "MAX_RESULT_ERRORS",
    "DETECT_MAX_DEFECTS_CAP",
    "RemixDetectMixDefectsEnqueueRequest",
    "RemixDetectMixDefectsSuccessData",
    "RemixDetectMixDefectsDedupData",
    "RemixDetectMixDefectsJobParams",
]


# Cap on result.errors[] to keep the job row small (lib contract — mirror job 11).
MAX_RESULT_ERRORS: int = 100

# Per-sheet defect cap ceiling (core default 30, hard cap 80 — parity with
# `DetectMixDefectsRequest.max_defects`).
DETECT_MAX_DEFECTS_CAP: int = 80


# ─── Request ───────────────────────────────────────────────────────────────


class RemixDetectMixDefectsEnqueueRequest(FlexibleModel):
    """Body for POST /api/jobs/remix/{remix_id}/detect-mix-defects.

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

    batch_id: str = Field(
        min_length=1,
        description="batch id (uuid) = mixes[].id. Scope = every SWAPPED crop sheet of this batch.",
    )
    force_resweep: bool = Field(
        default=True,
        description="carried for contract symmetry with mix-swap; detect does not persist, so it never gates scope (every swapped sheet is always inspected).",
    )
    # ── builder context (display-only — NOT used to call Gemini) ──
    swap_model: Optional[str] = Field(
        default=None,
        max_length=120,
        description="model used by the swap being inspected; rendered as context into the detect builder_params.",
    )
    swap_temperature: Optional[float] = Field(
        default=None,
        ge=0,
        le=2,
        description="temperature used by the swap being inspected; context only.",
    )
    # ── detect controls (forwarded per-sheet to the core) ──
    focus_objects: Optional[list[str]] = Field(
        default=None,
        description="restrict reported defects to these lineup tokens (must be a subset of the batch lineup).",
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


# ─── Response data variants (returned as dicts; models documentary) ─────────


class RemixDetectMixDefectsSuccessData(FlexibleModel):
    """201 Created — detect-mix job enqueued."""

    job_id: str
    status: Literal["queued"]
    type: Literal["remix_detect_mix_defects"]
    remix_id: str
    batch_id: str
    target_count: int
    total_steps: int
    sheets_to_process: int
    estimated_duration_sec: int


class RemixDetectMixDefectsDedupData(FlexibleModel):
    """409 JOB_ALREADY_ACTIVE — an active (queued|running) detect-mix job for this
    remix already exists. Surfaced inside the error envelope `details` so the FE
    can reuse the existing job. INDEPENDENT of the mix-swap + sprite-detect dedup
    families (distinct `type`) → all three can run concurrently."""

    job_id: str
    status: Literal["queued", "running"]
    type: Literal["remix_detect_mix_defects"]
    remix_id: str
    batch_id: str


# ─── Persisted JSONB shapes ─────────────────────────────────────────────────


class RemixDetectMixDefectsJobParams(FlexibleModel):
    """`background_jobs.params` shape (documentary)."""

    remix_id: str
    batch_id: str
    force_resweep: bool
    controls: dict
