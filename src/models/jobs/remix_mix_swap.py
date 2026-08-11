"""Pydantic models for `POST /api/jobs/remix/{remix_id}/mix-swap`.

Ported from image-api `src/models/jobs/remix_mix_swap.py`. Base class swapped
`FlexibleModel` → `pydantic.BaseModel` (the request model already pins
`extra="forbid"`, so behavior is identical). The 3 response variants are returned
as plain dicts from the router; these models exist for documentation + reuse.

Batch (mix) model:
  - no `variant_key` / `variants_processed` (a batch sheet has `variant_key=null`;
    the lineup's variants are derived from `original_crops[].tags[]`, not per sheet);
  - `batch_id` (= `mixes[].id` uuid); the swap lineup is derived at runtime from
    the aggregate `original_crops[].tags[]` (no stored keys[]);
  - `target_count` (the multi-target lineup, constant across every sheet).

⚡rev9: the crop pipeline is split into 3 stage jobs — swap (`mixes[]`, THIS job)
→ remove-bg (`rmbgs[]`, job 09) → upscale (`upscales[]`, job 10). This job's
post-swap pipeline is CUT-ONLY (native dim, NO resize) — no Replicate calls:
  - `result.errors[].stage` = 6 values: compose | swap | cut | persist | resolve
    | internal.
  - `swap_results[].crops[]` persists the LEAN shape
    `{spread_id, id, media_url, is_final?}` — geometry/tags joined from
    `original_crops[]` by `(spread_id, id)`.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.models.jobs.model_params_body import ModelParamsBody

__all__ = [
    "MAX_CONCURRENT_SHEETS",
    "MAX_RESULT_ERRORS",
    "PIPELINE_ERROR_CODES",
    "RemixMixSwapEnqueueRequest",
    "RemixMixSwapSuccessData",
    "RemixMixSwapSkippedData",
    "RemixMixSwapDedupData",
    "RemixMixSwapJobParams",
    "RemixMixSwapStepDetails",
    "RemixMixSwapResultError",
    "RemixMixSwapResult",
]


# ⚡rev9: sheets stay SEQUENTIAL (KISS + the Gemini semaphore cap=3 lives inside
# the primitive). This job no longer calls Replicate (cut-only post-swap).
MAX_CONCURRENT_SHEETS: int = 1

# Cap on result.errors[] to keep job row < 10 KB (lib contract).
MAX_RESULT_ERRORS: int = 100


# Sheet-fatal post-swap pipeline error codes (⚡rev9 — CUT-ONLY). Surfaced as
# `result.errors[].code` alongside `stage='cut'`.
PIPELINE_ERROR_CODES: frozenset[str] = frozenset({"CUT_FAILED"})


# ─── Request ───────────────────────────────────────────────────────────────


class RemixMixSwapEnqueueRequest(BaseModel):
    """Body for POST /api/jobs/remix/{remix_id}/mix-swap.

    `extra="forbid"` rejects unknown fields. `model_params` is a typed nested
    optional — omit → registry default (group `swap`, temp 0.25).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )

    batch_id: str = Field(
        min_length=1,
        description="batch id (uuid) = mixes[].id; swap lineup is derived from the aggregate original_crops[].tags[]. Scope = every crop sheet of this batch.",
    )
    force_resweep: bool = Field(
        default=False,
        description="true → clear + re-swap every sheet. false → idempotent skip of sheets with an is_selected swap.",
    )
    model_params: Optional[ModelParamsBody] = Field(
        default=None,
        description="optional model selection (group 'swap'); omit → default model + temp 0.25.",
    )


# ─── Response data variants (returned as dicts; models documentary) ─────────


class RemixMixSwapSuccessData(BaseModel):
    """201 Created — job enqueued."""

    job_id: str
    status: Literal["queued"]
    type: Literal["remix_mix_swap"]
    remix_id: str
    batch_id: str
    target_count: int
    total_steps: int
    sheets_to_process: int
    estimated_duration_sec: int


class RemixMixSwapSkippedData(BaseModel):
    """200 OK — precheck found 0 sheets in scope; no job row created."""

    skipped: Literal[True]
    reason: Literal["all_sheets_already_swapped", "no_crop_sheets"]
    sheets_to_process: Literal[0]


class RemixMixSwapDedupData(BaseModel):
    """200 OK — an active (queued|running) mix-swap job for this remix already
    exists. `active_swap_key` = the active job's `batch_id`.
    """

    job_id: str
    status: Literal["queued", "running"]
    type: Literal["remix_mix_swap"]
    remix_id: str
    active_swap_key: str
    deduped: Literal[True]


# ─── Persisted JSONB shapes ─────────────────────────────────────────────────


class RemixMixSwapJobParams(BaseModel):
    """`background_jobs.params` shape."""

    remix_id: str
    batch_id: str
    force_resweep: bool


class RemixMixSwapStepDetails(BaseModel):
    """`background_jobs.step_details` shape.

    - `sheets[sheet_index]`: state string OR failure object:
      - state strings: `pending` | `running` | `swap_done` | `done` |
        `skipped` | `cancelled`
      - failure object: `{state:'failed', stage, code?, message, target_key?}`
        where `stage` ∈ {compose, swap, cut, persist, resolve, internal}.
    - `step_timings[sheet_index]`: `{started_at, duration_ms}`.
    NOTE: no `variant_key` (mix sheets have `variant_key=null`).
    """

    sheets: dict[str, object]
    step_timings: dict[str, object] = Field(default_factory=dict)


class RemixMixSwapResultError(BaseModel):
    """Single entry inside `result.errors[]`.

    `stage` enum (⚡rev9 — 6 values):
      - from `run_swap_mix_sheet` (mapped via `_map_code_to_stage`):
        compose | swap | persist | resolve | internal
      - post-swap pipeline (CUT-ONLY): cut
    """

    stage: Literal[
        "compose",
        "swap",
        "cut",
        "persist",
        "resolve",
        "internal",
    ]
    sheet_index: int | None = None
    code: str | None = None
    message: str
    target_key: str | None = None


class RemixMixSwapResult(BaseModel):
    """`background_jobs.result` shape on terminal status."""

    batch_id: str
    target_count: int
    swapped_sheets: int
    skipped_sheets: int
    failed_sheets: int
    errors: list[RemixMixSwapResultError] = Field(default_factory=list)
