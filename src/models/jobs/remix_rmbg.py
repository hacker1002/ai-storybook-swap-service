"""Pydantic models for `POST /api/jobs/remix/{remix_id}/rmbg` (job 09).

Mirrors spec ai-storybook-design/api/jobs/09-enqueue-remix-rmbg.md
§Parameters, §Result, §Job Lifecycle Detail. The 3 response variants are
returned as plain dicts from the router (success 201, skipped 200, dedup 200) —
these models exist for documentation + reuse from the handler.

Crop pipeline stage 2 (swap `mixes[]` → REMOVE-BG `rmbgs[]` → upscale
`upscales[]`). Batch shape identical to `mixes[]`; `original_crops[]` =
copy-on-build clone of `mixes[]` finals (media_url = swapped raw cut @ NATIVE
dim). Per sheet: compose PLAIN (no ordinal, no stroke) → ONE remove-bg call per
sheet (`imageBytes`, bypasses the 10 MB decode cap) → cut N pieces → upload →
lean persist. The job writes ONLY the `rmbgs` JSONB column (disjoint from
`mixes`/`upscales` → runs in parallel with jobs 05/10 safely; dedup is
per-type `(remix_id, 'remix_rmbg')`).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field

from src.models.base import FlexibleModel
from src.models.jobs.model_params_body import ModelParamsBody

__all__ = [
    "MAX_CONCURRENT_SHEETS",
    "MAX_RESULT_ERRORS",
    "RMBG_ERROR_CODES",
    "RemixRmbgEnqueueRequest",
    "RemixRmbgSuccessData",
    "RemixRmbgSkippedData",
    "RemixRmbgDedupData",
    "RemixRmbgJobParams",
    "RemixRmbgStepDetails",
    "RemixRmbgResultError",
    "RemixRmbgResult",
]


# Sheets run SEQUENTIALLY — bounds Replicate in-flight to 1 (dev account is
# 1-call-at-a-time; the remove-bg call is the only Replicate hop per sheet).
# Cross-type contention with job 10 is bounded by the GLOBAL Replicate
# semaphore in `services/replicate_client.py`.
MAX_CONCURRENT_SHEETS: int = 1

# Cap on result.errors[] to keep job row < 10 KB (lib contract).
MAX_RESULT_ERRORS: int = 100


# Sheet-fatal error codes surfaced as `result.errors[].code`:
#   - ALL_CROPS_FAILED  stage=compose — every input crop fetch/decode failed
#                       (single-crop compose failures are graceful: the crop is
#                       dropped from cut/persist, `compose_skipped_count`++)
#   - RMBG_FAILED       stage=rmbg — the ONE remove-bg call per sheet failed
#                       (no graceful per-crop fallback — re-run is cheap, no
#                       Gemini cost; trade-off locked in spec 09)
#   - CUT_FAILED        stage=cut — RGBA sheet decode fail / ALL piece uploads
#                       failed (single piece upload fail → retry 1 → drop)
RMBG_ERROR_CODES: frozenset[str] = frozenset(
    {"ALL_CROPS_FAILED", "RMBG_FAILED", "CUT_FAILED"}
)


# ─── Request ───────────────────────────────────────────────────────────────


class RemixRmbgEnqueueRequest(FlexibleModel):
    """Body for POST /api/jobs/remix/{remix_id}/rmbg.

    `extra="forbid"` rejects unknown fields. `model_params` (Phase 02 wiring,
    group `rmbg`) is a typed nested optional — omit → default `bria/remove-background`.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )

    batch_id: str = Field(
        min_length=1,
        description="batch id (uuid) = rmbgs[].id. Scope = every crop sheet of this batch.",
    )
    force_resweep: bool = Field(
        default=False,
        description="true → clear + re-run every sheet. false → idempotent skip of sheets with an is_selected result.",
    )
    model_params: Optional[ModelParamsBody] = Field(
        default=None,
        description="optional model selection (group 'rmbg'); omit → bria/remove-background. No numeric params in v1.",
    )


# ─── Response data variants (returned as dicts; models documentary) ─────────


class RemixRmbgSuccessData(FlexibleModel):
    """201 Created — job enqueued. estimated_duration_sec ≈ sheets × ~15s
    (compose ~2s + rmbg ~5-10s + cut/upload ~2s)."""

    job_id: str
    status: Literal["queued"]
    type: Literal["remix_rmbg"]
    remix_id: str
    batch_id: str
    total_steps: int
    sheets_to_process: int
    estimated_duration_sec: int


class RemixRmbgSkippedData(FlexibleModel):
    """200 OK — precheck found 0 sheets in scope; no job row created."""

    skipped: Literal[True]
    reason: Literal["all_sheets_already_done", "no_crop_sheets"]
    sheets_to_process: Literal[0]


class RemixRmbgDedupData(FlexibleModel):
    """200 OK — an active (queued|running) rmbg job for this remix already
    exists (dedup per-type — independent of jobs 05/10). `active_key` = the
    active job's `batch_id`.
    """

    job_id: str
    status: Literal["queued", "running"]
    type: Literal["remix_rmbg"]
    remix_id: str
    active_key: str
    deduped: Literal[True]


# ─── Persisted JSONB shapes ─────────────────────────────────────────────────


class RemixRmbgJobParams(FlexibleModel):
    """`background_jobs.params` shape."""

    remix_id: str
    batch_id: str
    force_resweep: bool


class RemixRmbgStepDetails(FlexibleModel):
    """`background_jobs.step_details` shape.

    - `sheets[sheet_index]`: state string OR done-meta OR failure object:
      - state strings: `pending` | `running` | `rmbg_done` (heartbeat #1,
        post remove-bg / pre-cut) | `done` | `skipped` | `cancelled`
      - done-meta (≥1 crop fetch/decode failed at compose — dropped from the
        output): `{state:'done', compose_skipped_count: int}`
      - failure object: `{state:'failed', stage, code?, message}` where
        `stage` ∈ {compose, rmbg, cut, persist, internal}.
    - `step_timings[sheet_index]`: `{started_at, duration_ms}`.
    Stored as untyped dict — Pydantic only used as a docstring carrier here.
    """

    sheets: dict[str, object]
    step_timings: dict[str, object] = Field(default_factory=dict)


class RemixRmbgResultError(FlexibleModel):
    """Single entry inside `result.errors[]`."""

    stage: Literal["compose", "rmbg", "cut", "persist", "internal"]
    sheet_index: int | None = None
    code: str | None = None
    message: str


class RemixRmbgResult(FlexibleModel):
    """`background_jobs.result` shape on terminal status."""

    batch_id: str
    processed_sheets: int
    skipped_sheets: int
    failed_sheets: int
    errors: list[RemixRmbgResultError] = Field(default_factory=list)
