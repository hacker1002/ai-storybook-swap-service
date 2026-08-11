"""Pydantic models for POST /api/remix/detect-mix-defects.

Spec: ai-storybook-design/api/remix/07-detect-mix-defects.md (AUTHORITATIVE).

Mix-swap defect localization — image-IN / defect-regions-OUT. Sibling of 06
(`detect_swap_defects`) for the MIX plane. The request is a SUPERSET of the
mix-swap body (04): the caller echoes the exact body it sent to
`swap-mix-crop-sheet` (the reference for "what should the result look like") +
the SWAPPED `result_crops[]` to recompose + a few optional controls. `Crop`
(== MixCrop) / `SwapTarget` / `SheetGeometry` are reused 1:1 from the swap
models (DRY — NO crop/target model redefined). `DefectPoint` / `DefectBox` /
`SwappedDimensions` are reused 1:1 from the 06 model; only `SwapDefect.category`
differs (MixDefectCategory — 10 mix-adapted reasons).

RESULT (parity 06): the inspected RESULT is RECOMPOSED in-process from
`result_crops[]` (the per-cell swapped pieces) via the SAME `compose_crop_sheet`
as the ORIGINAL → the two sheets are pixel-aligned (`swappedDimensions ==
sheet_geometry`).

HTTP-code policy (memory `reference_image_api_validation_http_codes`): every
body/cross-field failure is raised as `RemixDomainError(status=400, ...)` so the
global handler emits the spec envelope at 400. `max_defects` range is a plain
`Field(ge,le)` → 422 `RequestValidationError` → global handler normalizes to
400 VALIDATION_ERROR. This sync endpoint binds the body DIRECTLY to the core
req, so every numeric control (`max_defects`) is a PUBLIC input and is bounded
at the schema layer (it skips any job-path registry clamp).

Stateless / advisory — the response carries the located defect regions only; no
persistence, no image edit, no re-swap.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.requests._attribution import RemixId
# Reuse the crop/geometry models 1:1 (DRY — caller echoes the swap body). `Crop`
# is the MixCrop shape (id/media_url/geometry/annotation?/objects?); SheetGeometry
# already enforces SHEET_TOO_LARGE bounds in its own validator.
from src.models.requests.build_crop_sheet import (
    MAX_CROPS,
    Crop,
    SheetGeometry,
)

# Reuse SwapTarget (key + reference/target_base URLs + object_context) from 04.
from src.models.requests.swap_mix_crop_sheet import SwapTarget

# Reuse the response leaf shapes from 06 verbatim (identical px circle contract).
from src.models.requests.detect_swap_defects import (
    DefectBox,
    DefectPoint,
    SwappedDimensions,
)
from src.services.gemini.payload_budget import MAX_SWAP_TARGETS
from src.services.remix.errors import RemixDomainError

__all__ = [
    # constants
    "MAX_CROPS",
    "MAX_SWAP_TARGETS",
    "MAX_IMAGE_BYTES",
    "MAX_ORIGINAL_SHEET_BYTES",
    "MAX_DEFECTS_DEFAULT",
    "MAX_DEFECTS_CAP",
    "MAX_DEFECT_MESSAGE_LEN",
    "DETECT_MIX_DEFECTS_SYSTEM_NAME",
    "DETECT_MIX_DEFECTS_DEFAULT_MODEL",
    "DETECT_TEMPERATURE",
    "DETECT_TIMEOUT_S",
    "MAX_DETECT_RETRIES",
    "MIX_DEFECT_CATEGORIES",
    "MixDefectCategory",
    "MixDefectSeverity",
    # reused leaves
    "DefectPoint",
    "DefectBox",
    "SwappedDimensions",
    # request
    "DetectMixDefectsRequest",
    # response
    "SwapDefect",
    "DetectMixDefectsData",
    "DetectMixDefectsMeta",
    "DetectMixDefectsResponse",
]

# ─────────────────────────────── constants ─────────────────────────────────

MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10MB/image (crop piece / reference NEW / target_base OLD)
# Fetch cap for the `original_sheet_url` FAST-PATH only (a full composed mix sheet
# can exceed 10MB). Crop pieces (composer-fetched) keep the 10MB single-image cap.
MAX_ORIGINAL_SHEET_BYTES: int = 32 * 1024 * 1024  # 32MB — original_sheet_url fast-path DoS bound
MAX_DEFECTS_DEFAULT: int = 30
MAX_DEFECTS_CAP: int = 80
MAX_DEFECT_MESSAGE_LEN: int = 500  # cap echoed Gemini message (anti prompt-bloat / PII safety)

DETECT_MIX_DEFECTS_SYSTEM_NAME: str = "DETECT_MIX_DEFECTS_SYSTEM"
DETECT_MIX_DEFECTS_DEFAULT_MODEL: str = "gemini-3-flash-preview"  # 2026-06-29: ↑sensitivity for SUBTLE defects (was gemini-3.5-flash); live model comes from prompt_templates.model, this is the fallback
DETECT_TEMPERATURE: float = 0.1  # factual localization, reduce false-positive over-flag
DETECT_TIMEOUT_S: float = 90.0
MAX_DETECT_RETRIES: int = 2  # langchain transient-retry (429/5xx); app does 1 parse-retry

# Single source for the 10 MIX-adapted defect categories — drives the response
# Literal + the core's drop-invalid filter. Diverges from 06: `trait_leak`
# (per-trait) → `unrelated_object_changed` (full-identity mix).
MIX_DEFECT_CATEGORIES: tuple[str, ...] = (
    "identity_mismatch",
    "cross_contamination",
    "not_swapped",
    "unrelated_object_changed",
    "pose_or_composition",
    "art_style_break",
    "ordinal_altered",
    "cell_structure",
    "artifact",
    "other",
)
MixDefectCategory = Literal[
    "identity_mismatch",
    "cross_contamination",
    "not_swapped",
    "unrelated_object_changed",
    "pose_or_composition",
    "art_style_break",
    "ordinal_altered",
    "cell_structure",
    "artifact",
    "other",
]
MixDefectSeverity = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ─────────────────────────────── request model ─────────────────────────────


class DetectMixDefectsRequest(_Strict):
    """Superset of the mix-swap body (04) + result pieces + optional controls.

    `crops` (== MixCrop) / `swap_targets` / `result_crops` reuse the swap models
    verbatim — the caller echoes the body it sent to `04-swap-mix-crop-sheet` +
    the per-cell SWAPPED pieces. `swap_model` / `swap_temperature` are CONTEXT
    only (rendered into `builder_params`, NEVER used to call Gemini — the detect
    call hardcodes a factual temperature). `original_sheet_url` is a fast-path:
    present → use as the ORIGINAL sheet (skip compose + skip fetching
    `crops[].media_url`); absent → compose from `crops[]`. The RESULT is ALWAYS
    recomposed from `result_crops[]` via the same composer (→ pixel-aligned).
    """

    # ── echo of the swap body (references to judge the result against) ──
    sheet_geometry: SheetGeometry
    crops: list[Crop]
    swap_targets: list[SwapTarget]

    # ── builder params already used (context only) ──
    # Bounded length even though display-only: it IS rendered verbatim into the
    # Gemini `builder_params` text, so cap it (anti prompt-bloat / injection).
    swap_model: Optional[str] = Field(default=None, max_length=120)
    # CONTEXT only — rendered as display text, never forwarded as the detect call
    # temperature. Still bounded [0,2] for defense-in-depth: this body binds
    # straight to the core req, so the field is a public input.
    swap_temperature: Optional[float] = Field(default=None, ge=0, le=2)

    # ── result to inspect (per-cell SWAPPED pieces → core recomposes the sheet) ──
    result_crops: list[Crop]

    # ── optional fast-path + controls ──
    original_sheet_url: Optional[str] = Field(default=None, pattern=r"^https?://")
    focus_objects: Optional[list[str]] = None
    severity_threshold: Optional[MixDefectSeverity] = None  # default 'low' applied in core
    max_defects: int = Field(default=MAX_DEFECTS_DEFAULT, ge=1, le=MAX_DEFECTS_CAP)
    # AI-usage attribution (Phase 05) — OPTIONAL remix id (billing DISCRIMINATOR).
    # Sync router stamps `AiCallContext(remix_id=remixId)`; the job path injects
    # `ai_context` from the job row instead. `extra="forbid"` → declared, not dropped.
    remixId: Optional[RemixId] = None

    @model_validator(mode="after")
    def _check_business_limits(self) -> "DetectMixDefectsRequest":
        sw = self.sheet_geometry.width
        sh = self.sheet_geometry.height

        # ── 1. crops: non-empty, ≤ MAX_CROPS, geometry within sheet ──
        n = len(self.crops)
        if n == 0:
            raise RemixDomainError(
                status=400, code="EMPTY_CROPS",
                message="crops[] must contain at least 1 item",
            )
        if n > MAX_CROPS:
            raise RemixDomainError(
                status=400, code="TOO_MANY_CROPS",
                message=f"crops[] length {n} exceeds maximum {MAX_CROPS}",
                details={"count": n, "max": MAX_CROPS},
            )
        for idx, c in enumerate(self.crops):
            g = c.geometry
            if g.x + g.w > sw or g.y + g.h > sh:
                raise RemixDomainError(
                    status=400, code="GEOMETRY_OUT_OF_BOUNDS",
                    message=(
                        f"crop[{idx}] id={c.id} geometry "
                        f"({g.x},{g.y},{g.w}x{g.h}) exceeds sheet {sw}x{sh}"
                    ),
                    details={"index": idx, "id": c.id},
                )

        # ── 1b. result_crops: non-empty, ≤ MAX_CROPS, geometry within sheet ──
        rn = len(self.result_crops)
        if rn == 0:
            raise RemixDomainError(
                status=400, code="EMPTY_RESULT_CROPS",
                message="result_crops[] must contain at least 1 item",
            )
        if rn > MAX_CROPS:
            raise RemixDomainError(
                status=400, code="TOO_MANY_CROPS",
                message=f"result_crops[] length {rn} exceeds maximum {MAX_CROPS}",
                details={"count": rn, "max": MAX_CROPS, "field": "result_crops"},
            )
        for idx, c in enumerate(self.result_crops):
            g = c.geometry
            if g.x + g.w > sw or g.y + g.h > sh:
                raise RemixDomainError(
                    status=400, code="GEOMETRY_OUT_OF_BOUNDS",
                    message=(
                        f"result_crop[{idx}] id={c.id} geometry "
                        f"({g.x},{g.y},{g.w}x{g.h}) exceeds sheet {sw}x{sh}"
                    ),
                    details={"index": idx, "id": c.id, "field": "result_crops"},
                )

        # ── 2. swap_targets: non-empty, ≤ MAX_SWAP_TARGETS ──
        targets = self.swap_targets
        if len(targets) == 0:
            raise RemixDomainError(
                status=400, code="EMPTY_SWAP_TARGETS",
                message="swap_targets[] must contain at least 1 item",
            )
        if len(targets) > MAX_SWAP_TARGETS:
            raise RemixDomainError(
                status=400, code="TOO_MANY_SWAP_TARGETS",
                message=(
                    f"swap_targets[] length {len(targets)} exceeds maximum "
                    f"{MAX_SWAP_TARGETS}"
                ),
                details={"count": len(targets), "max": MAX_SWAP_TARGETS},
            )

        # ── 3. key uniqueness ──
        keys = [t.key for t in targets]
        if len(set(keys)) != len(keys):
            seen: set[str] = set()
            dup: Optional[str] = None
            for k in keys:
                if k in seen:
                    dup = k
                    break
                seen.add(k)
            raise RemixDomainError(
                status=400, code="DUPLICATE_TARGET_KEY",
                message="swap_targets[].key must be unique within the request",
                details={"key": dup},
            )

        # ── 4. per-target URL schemes + non-empty name ──
        #     (reference_image_url min_length=1 already enforced by SwapTarget.)
        for idx, t in enumerate(targets):
            if not (
                t.reference_image_url.startswith("http://")
                or t.reference_image_url.startswith("https://")
            ):
                raise RemixDomainError(
                    status=400, code="VALIDATION_ERROR",
                    message="reference_image_url must start with http(s)://",
                    details={"field": "reference_image_url", "index": idx, "target_key": t.key},
                )
            if t.target_base_image_url is not None and not (
                t.target_base_image_url.startswith("http://")
                or t.target_base_image_url.startswith("https://")
            ):
                raise RemixDomainError(
                    status=400, code="VALIDATION_ERROR",
                    message="target_base_image_url must start with http(s)://",
                    details={"field": "target_base_image_url", "index": idx, "target_key": t.key},
                )
            if not (t.object_context.name or "").strip():
                raise RemixDomainError(
                    status=400, code="VALIDATION_ERROR",
                    message="swap_targets[].object_context.name must be non-empty",
                    details={"field": "object_context.name", "index": idx, "target_key": t.key},
                )

        # ── 5. focus_objects ⊆ target keys ──
        if self.focus_objects is not None:
            valid = set(keys)
            unknown = sorted({k for k in self.focus_objects if k not in valid})
            if unknown:
                raise RemixDomainError(
                    status=400, code="VALIDATION_ERROR",
                    message="focus_objects must be a subset of swap_targets[].key",
                    details={"focus_objects": unknown},
                )

        return self


# ─────────────────────────────── response models ───────────────────────────


class SwapDefect(BaseModel):
    """One located defect region on the RESULT image (px, basis = swappedDimensions).

    Reuses the 06 shape verbatim (`center`+`radius`+`box`+annotations); the ONLY
    difference is `category` ∈ `MixDefectCategory` (10 mix-adapted reasons). All
    annotation fields are optional. `cell` = CROP-cell ordinal (NOT target/variant
    ordinal); `object_key` = the related target key.
    """

    center: DefectPoint
    radius: int
    box: Optional[DefectBox] = None
    category: Optional[MixDefectCategory] = None
    severity: Optional[MixDefectSeverity] = None
    message: Optional[str] = None
    confidence: Optional[float] = None
    cell: Optional[int] = None
    object_key: Optional[str] = None


class DetectMixDefectsData(BaseModel):
    defects: list[SwapDefect]


class DetectMixDefectsMeta(BaseModel):
    cellCount: int
    targetCount: int
    defectCount: int
    rawDefectCount: Optional[int] = None
    truncated: Optional[bool] = None
    swappedDimensions: SwappedDimensions
    hasOldVariantSheet: Optional[bool] = None
    processingTimeMs: Optional[int] = None
    tokenUsage: Optional[int] = None


class DetectMixDefectsResponse(BaseModel):
    success: bool
    data: DetectMixDefectsData
    meta: Optional[DetectMixDefectsMeta] = None
