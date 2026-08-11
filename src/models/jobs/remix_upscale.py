"""Pydantic models for `POST /api/jobs/remix/{remix_id}/upscale` (job 10).

Mirrors spec ai-storybook-design/api/jobs/10-enqueue-remix-upscale.md
§Parameters, §Result, §Job Lifecycle Detail. The 3 response variants are
returned as plain dicts from the router — these models exist for documentation
+ reuse from the handler.

Crop pipeline stage 3/FINAL (swap `mixes[]` → remove-bg `rmbgs[]` → UPSCALE
`upscales[]`). `original_crops[]` = copy-on-build clone of `rmbgs[]` finals
(media_url = bg-removed RGBA piece @ NATIVE dim). Per sheet the job upscales
EACH crop INDEPENDENTLY (NO combine, NO cut — upscaling changes per-piece
dims): fetch piece → resolve PRINT target from the SOURCE illustration layer
geometry keyed `(spread_id, id)` (`× PRINT_UPSCALE_FACTOR` → 300 DPI;
`original_crops[].geometry` here = native piece dims and is NOT a layout box)
→ real-esrgan `faceEnhance=true` (scale ≤ 1 → Pillow; call fail → graceful
fallback pre-upscale + `upscale_skipped_count`) → upload → lean persist.
`swap_results[].media_url = null` (per-crop processing, no sheet output). The
job writes ONLY the `upscales` JSONB column; dedup is per-type. Finals of THIS
stage are the Inject Phase 3 source (client-side).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field

from src.models.base import FlexibleModel
from src.models.jobs.model_params_body import ModelParamsBody
from src.models.requests.upscale_image import (
    GRAIN_AMP_MAX,
    GRAIN_BLUR_MAX,
    GRAIN_DEFAULT_AMP,
    GRAIN_DEFAULT_BLUR,
    GRAIN_DEFAULT_SEED,
)

__all__ = [
    "MAX_CONCURRENT_SHEETS",
    "MAX_CONCURRENT_UPSCALE_CROPS",
    "PRINT_UPSCALE_FACTOR",
    "MAX_UPSCALE_SCALE",
    "MAX_RESULT_ERRORS",
    "UPSCALE_ERROR_CODES",
    "RemixUpscaleGrainBody",
    "normalize_grain",
    "RemixUpscaleEnqueueRequest",
    "RemixUpscaleSuccessData",
    "RemixUpscaleSkippedData",
    "RemixUpscaleDedupData",
    "RemixUpscaleJobParams",
    "RemixUpscaleStepDetails",
    "RemixUpscaleResultError",
    "RemixUpscaleResult",
]


# ─── Grain (top-level body knob, model-agnostic — NOT model_params) ──────────


class RemixUpscaleGrainBody(FlexibleModel):
    """Watercolor-grain knobs on the job-10 body. PERMISSIVE (no Field bounds):
    unlike the sync endpoint (`GrainParams`, which rejects out-of-range with 400),
    a service-to-service stage job CLAMPS at normalize so a bad knob never fails
    the enqueue of an entire batch. `seed` is the base seed — the handler adds
    `crop_idx` per crop for per-cell variation. Omit the object (or enabled=false)
    → grain off."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    amp: Optional[float] = None
    blur: Optional[float] = None
    seed: Optional[int] = None


def normalize_grain(grain: "RemixUpscaleGrainBody | None") -> Optional[dict]:
    """Router-side normalize → the persisted `params.grain` dict, or None.

    omit / disabled → None (back-compat: handler treats absence as off). Provided
    + enabled → `{enabled:true, amp:clamp[0,50], blur:clamp[0,5], seed:int}` with
    defaults filled for any omitted knob. Clamp (not reject) per spec 10 — robust
    against a bad service caller.
    """
    if grain is None or not grain.enabled:
        return None
    amp = GRAIN_DEFAULT_AMP if grain.amp is None else float(grain.amp)
    blur = GRAIN_DEFAULT_BLUR if grain.blur is None else float(grain.blur)
    seed = GRAIN_DEFAULT_SEED if grain.seed is None else int(grain.seed)
    amp = min(max(amp, 0.0), GRAIN_AMP_MAX)
    blur = min(max(blur, 0.0), GRAIN_BLUR_MAX)
    seed = max(0, seed)  # numpy default_rng rejects negatives; keep seed+crop_idx ≥ 0
    return {"enabled": True, "amp": amp, "blur": blur, "seed": seed}


# `MAX_CONCURRENT_SHEETS × MAX_CONCURRENT_UPSCALE_CROPS = 1` → Replicate
# in-flight 1 within this job (dev account is 1-call-at-a-time). Raising the
# account tier later → bump both (product = budget). Cross-type contention
# with job 09 is bounded by the GLOBAL Replicate semaphore in
# `services/replicate_client.py`.
MAX_CONCURRENT_SHEETS: int = 1
MAX_CONCURRENT_UPSCALE_CROPS: int = 1

# Print-quality target (moved from job 05 rev7.1 — ⚡rev9 2026-06-12, source
# changed): the SOURCE illustration layer geometry is authored at 1/4 of print
# @300dpi (FE `DIMENSION_PAGE_SIZE`/`DIMENSION_CANVAS_SIZE`) → print target =
# `illustration.spreads[].images[].geometry.{w,h} × PRINT_UPSCALE_FACTOR`,
# resolved by `(spread_id, id)`. NOT derived from `original_crops[].geometry`
# (= native piece dims at this stage). Constant ×4 locked v1 (plan Validation
# S1 — never duplicate the FE dimension table in Python); verify bleed scale
# at Final Step.
PRINT_UPSCALE_FACTOR: float = 4.0

# Upscale scale ceiling — `scale = max(print_w/piece_w, print_h/piece_h)` is
# clamped here (+ log.warn). The native chain means scale can exceed ×4 when
# the swap scaled pieces down (Gemini ~2K output cap on large sheets).
MAX_UPSCALE_SCALE: float = 10.0

# Cap on result.errors[] to keep job row < 10 KB (lib contract).
MAX_RESULT_ERRORS: int = 100


# Sheet-fatal error codes surfaced as `result.errors[].code`:
#   - ALL_CROP_PIPELINES_FAILED  stage=crops — EVERY crop fetch/upload failed
#     (rare — Storage outage). A failed upscale CALL is NOT an error: graceful
#     fallback re-uploads the pre-upscale piece (kept dim) and increments
#     `upscale_skipped_count` (done-meta + result), never `errors[]`.
UPSCALE_ERROR_CODES: frozenset[str] = frozenset({"ALL_CROP_PIPELINES_FAILED"})


# ─── Request ───────────────────────────────────────────────────────────────


class RemixUpscaleEnqueueRequest(FlexibleModel):
    """Body for POST /api/jobs/remix/{remix_id}/upscale.

    `extra="forbid"` rejects unknown fields. `model_params` (Phase 02 wiring,
    group `upscale`) selects the model + quality knobs ONLY — NEVER `scale`
    (always derived from geometry, PRINT 300 DPI). v1 dispatches only
    `nightmareai/real-esrgan` (default); recraft/alexgenovese are registered but
    NOT_SUPPORTED → 422 at enqueue (deferred). Omit → real-esrgan + face_enhance true.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )

    batch_id: str = Field(
        min_length=1,
        description="batch id (uuid) = upscales[].id. Scope = every crop sheet of this batch.",
    )
    force_resweep: bool = Field(
        default=False,
        description="true → clear + re-run every sheet. false → idempotent skip of sheets with an is_selected result.",
    )
    model_params: Optional[ModelParamsBody] = Field(
        default=None,
        description="optional model selection (group 'upscale'); omit → real-esrgan + face_enhance. NEVER controls scale (geometry-derived). recraft/alexgenovese → 422 (deferred v1).",
    )
    grain: Optional[RemixUpscaleGrainBody] = Field(
        default=None,
        description="optional watercolor grain (top-level, model-agnostic; NOT model_params). Omit/disabled → off. Applied per-crop AFTER normalize-resize; seed offset by crop_idx.",
    )


# ─── Response data variants (returned as dicts; models documentary) ─────────


class RemixUpscaleSuccessData(FlexibleModel):
    """201 Created — job enqueued. estimated_duration_sec ≈ Σ sheets
    (N_crops × ~15s) — upscale nearly ALWAYS runs (300 DPI target → scale > 1;
    scale ≤ 1 Pillow path is rare/instant)."""

    job_id: str
    status: Literal["queued"]
    type: Literal["remix_upscale"]
    remix_id: str
    batch_id: str
    total_steps: int
    sheets_to_process: int
    estimated_duration_sec: int


class RemixUpscaleSkippedData(FlexibleModel):
    """200 OK — precheck found 0 sheets in scope; no job row created."""

    skipped: Literal[True]
    reason: Literal["all_sheets_already_done", "no_crop_sheets"]
    sheets_to_process: Literal[0]


class RemixUpscaleDedupData(FlexibleModel):
    """200 OK — an active (queued|running) upscale job for this remix already
    exists (dedup per-type — independent of jobs 05/09). `active_key` = the
    active job's `batch_id`.
    """

    job_id: str
    status: Literal["queued", "running"]
    type: Literal["remix_upscale"]
    remix_id: str
    active_key: str
    deduped: Literal[True]


# ─── Persisted JSONB shapes ─────────────────────────────────────────────────


class RemixUpscaleJobParams(FlexibleModel):
    """`background_jobs.params` shape."""

    remix_id: str
    batch_id: str
    force_resweep: bool
    # Normalized grain (audit/replay): `{enabled, amp, blur, seed}` when grain was
    # enabled at enqueue, else absent. Handler reads `params.get("grain")`.
    grain: Optional[dict] = None


class RemixUpscaleStepDetails(FlexibleModel):
    """`background_jobs.step_details` shape.

    - `sheets[sheet_index]`: state string OR processing/done-meta OR failure:
      - state strings: `pending` | `running` | `done` | `skipped` | `cancelled`
      - processing-meta (heartbeat after EVERY crop — Replicate calls are
        long): `{state:'processing', crops_done: int, crops_total: int}`
      - done-meta (≥1 crop fell back to its pre-upscale bytes — graceful):
        `{state:'done', upscale_skipped_count: int}`
      - failure object: `{state:'failed', stage, code?, message}` where
        `stage` ∈ {crops, persist, internal} (NO compose/swap/cut — this job
        only has the per-crop pipeline).
    - `step_timings[sheet_index]`: `{started_at, duration_ms}`.
    Stored as untyped dict — Pydantic only used as a docstring carrier here.
    """

    sheets: dict[str, object]
    step_timings: dict[str, object] = Field(default_factory=dict)


class RemixUpscaleResultError(FlexibleModel):
    """Single entry inside `result.errors[]`."""

    stage: Literal["crops", "persist", "internal"]
    sheet_index: int | None = None
    code: str | None = None
    message: str


class RemixUpscaleResult(FlexibleModel):
    """`background_jobs.result` shape on terminal status."""

    batch_id: str
    processed_sheets: int
    skipped_sheets: int
    failed_sheets: int
    upscale_skipped_count: int = 0
    # Present only when grain was enabled — count of crops where grain failed or
    # exceeded GRAIN_MAX_PIXELS (non-fatal: the crop still uploaded ungrained).
    grain_skipped_count: Optional[int] = None
    errors: list[RemixUpscaleResultError] = Field(default_factory=list)
