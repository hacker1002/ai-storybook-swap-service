"""Pydantic models for `POST /api/jobs/remix/{remix_id}/detect-rmbg-defects`.

Enqueue model for the RMBG-plane remove-bg defect-detection job
(`remix_detect_rmbg_defects`) — 3rd plane of the detect family (after sprite
job 11 + mix job 12). Orchestrates the AI core `run_detect_rmbg_defects()`
([api/remix/08]) over every crop sheet of ONE rmbg BATCH
(`remixes.rmbgs[]`, identified by `id`) that already carries a selected remove-bg
result.

THE SIMPLEST detect enqueue model (spec 13): scoped by `batch_id` (= `rmbgs[].id`),
resolve reads ONLY `rmbgs[]` — NO target pool / annotation_map / lineup → so there
is NO `swap_model` / `swap_temperature` / `focus_objects` (rmbg does not swap an
identity) and NO `MISSING_OBJECT_CONFIG` precondition. Only the two detect controls
(`severity_threshold` / `max_defects` 1..80) are forwarded per-sheet to the core.

Defects are ADVISORY / ephemeral: the handler writes them to
`background_jobs.result.defectsBySheet`, NEVER to `remixes`. The documentary
response models exist for reuse + doc parity; the router returns plain dicts.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field

from src.models.base import FlexibleModel

__all__ = [
    "MAX_RESULT_ERRORS",
    "DETECT_MAX_DEFECTS_CAP",
    "RemixDetectRmbgDefectsEnqueueRequest",
    "RemixDetectRmbgDefectsSuccessData",
    "RemixDetectRmbgDefectsDedupData",
    "RemixDetectRmbgDefectsJobParams",
]


# Cap on result.errors[] to keep the job row small (lib contract — mirror job 11/12).
MAX_RESULT_ERRORS: int = 100

# Per-sheet defect cap ceiling (core default 30, hard cap 80 — parity with
# `DetectRmbgDefectsRequest.max_defects`).
DETECT_MAX_DEFECTS_CAP: int = 80


# ─── Request ───────────────────────────────────────────────────────────────


class RemixDetectRmbgDefectsEnqueueRequest(FlexibleModel):
    """Body for POST /api/jobs/remix/{remix_id}/detect-rmbg-defects.

    `extra="forbid"` rejects unknown fields. The detect controls are all optional
    — omit → core defaults (severity 'low', max_defects 30). `max_defects` is
    bounded 1..80 at the schema layer (out-of-range → Pydantic body error → the
    global handler emits 400 VALIDATION_ERROR). Minimal body: NO swap_model /
    swap_temperature / focus_objects / backing_color (rmbg only inspects the
    cutout mask).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )

    batch_id: str = Field(
        min_length=1,
        description="batch id (uuid) = rmbgs[].id. Scope = every crop sheet of this batch that has a selected remove-bg result.",
    )
    force_resweep: bool = Field(
        default=True,
        description="carried for contract symmetry with rmbg-swap; detect does not persist, so it never gates scope (every selected sheet is always inspected).",
    )
    # ── detect controls (forwarded per-sheet to the core) ──
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


class RemixDetectRmbgDefectsSuccessData(FlexibleModel):
    """201 Created — detect-rmbg job enqueued."""

    job_id: str
    status: Literal["queued"]
    type: Literal["remix_detect_rmbg_defects"]
    remix_id: str
    batch_id: str
    total_steps: int
    sheets_to_process: int
    estimated_duration_sec: int


class RemixDetectRmbgDefectsDedupData(FlexibleModel):
    """409 JOB_ALREADY_ACTIVE — an active (queued|running) detect-rmbg job for this
    remix already exists. Surfaced inside the error envelope `details` so the FE
    can reuse the existing job. INDEPENDENT of the rmbg-swap (job 09) + detect-mix
    (job 12) + detect-sprite (job 11) dedup families (distinct `type`) → all run
    concurrently."""

    job_id: str
    status: Literal["queued", "running"]
    type: Literal["remix_detect_rmbg_defects"]
    remix_id: str
    batch_id: str


# ─── Persisted JSONB shapes ─────────────────────────────────────────────────


class RemixDetectRmbgDefectsJobParams(FlexibleModel):
    """`background_jobs.params` shape (documentary)."""

    remix_id: str
    batch_id: str
    force_resweep: bool
    controls: dict
