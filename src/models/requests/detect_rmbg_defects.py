"""Pydantic models for POST /api/remix/detect-rmbg-defects.

Spec: ai-storybook-design/api/remix/08-detect-rmbg-defects.md (AUTHORITATIVE).

Remove-BG defect localization — image-IN / defect-regions-OUT. 3rd plane of the
detect family (after sprite 06 + mix 07). THE SIMPLEST: 2 images of ONE crop
sheet — the ORIGINAL still-background sheet (opaque) vs the RESULT cut-out sheet
(RGBA transparent) — one Gemini call locates wrong/poor BACKGROUND-REMOVAL
regions on the RESULT. NO identity / human-ref / variant-sheet / swap_plan /
swap_objects / backing_color (it only inspects the alpha MASK, not the content).

`RmbgCrop` is a LEAN crop (id/media_url/geometry only — NO annotation/objects;
there is no manifest). `DefectPoint` / `DefectBox` / `SwappedDimensions` are
reused 1:1 from the 06 model; only the defect MODEL differs: `RmbgDefect` has a
`category` ∈ `RmbgDefectCategory` (7 rmbg-specific reasons) and DROPS `object_key`
(rmbg has no swap target).

RESULT (parity 06/07): the inspected RESULT is RECOMPOSED in-process from
`result_crops[]` via the SAME `compose_crop_sheet` as the ORIGINAL (transparent
canvas mode → alpha preserved) → the two sheets are pixel-aligned
(`swappedDimensions == sheet_geometry`).

HTTP-code policy (memory `reference_image_api_validation_http_codes`): every
body/cross-field failure is raised as `RemixDomainError(status=400, ...)` so the
global handler emits the spec envelope at 400. `max_defects` range is a plain
`Field(ge,le)` → 422 `RequestValidationError` → global handler normalizes to 400
VALIDATION_ERROR. This sync endpoint binds the body DIRECTLY to the core req, so
every numeric control (`max_defects`) is a PUBLIC input bounded at the schema
layer.

Stateless / advisory — the response carries the located defect regions only; no
persistence, no image edit, no re-run rmbg.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.requests._attribution import RemixId
# Reuse the geometry/sheet models 1:1 (DRY). `SheetGeometry` already enforces
# SHEET_TOO_LARGE bounds in its own validator (dim + pixel caps for free).
from src.models.requests.build_crop_sheet import (
    MAX_CROPS,
    Geometry,
    SheetGeometry,
)

# Reuse the response leaf shapes from 06 verbatim (identical px circle contract).
from src.models.requests.detect_swap_defects import (
    DefectBox,
    DefectPoint,
    SwappedDimensions,
)
from src.services.remix.errors import RemixDomainError

__all__ = [
    # constants
    "MAX_CROPS",
    "MAX_IMAGE_BYTES",
    "MAX_SHEET_FETCH_BYTES",
    "MAX_DEFECTS_DEFAULT",
    "MAX_DEFECTS_CAP",
    "MAX_DEFECT_MESSAGE_LEN",
    "DETECT_RMBG_DEFECTS_SYSTEM_NAME",
    "DETECT_RMBG_DEFECTS_DEFAULT_MODEL",
    "DETECT_TEMPERATURE",
    "DETECT_TIMEOUT_S",
    "MAX_DETECT_RETRIES",
    "RMBG_DEFECT_CATEGORIES",
    "RmbgDefectCategory",
    "RmbgDefectSeverity",
    # reused leaves
    "DefectPoint",
    "DefectBox",
    "SwappedDimensions",
    # request
    "RmbgCrop",
    "DetectRmbgDefectsRequest",
    # response
    "RmbgDefect",
    "DetectRmbgDefectsData",
    "DetectRmbgDefectsMeta",
    "DetectRmbgDefectsResponse",
]

# ─────────────────────────────── constants ─────────────────────────────────

MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10MB/image (crop piece — composer-fetched)
# Fetch cap for the sheet FAST-PATHS (`original_sheet_url` / `result_sheet_url`):
# a full composed sheet can exceed 10MB. Crop pieces keep the 10MB single-image
# cap. `Image.MAX_IMAGE_PIXELS` still guards decompression bombs at decode time.
MAX_SHEET_FETCH_BYTES: int = 32 * 1024 * 1024  # 32MB — sheet fast-path DoS bound
MAX_DEFECTS_DEFAULT: int = 30
MAX_DEFECTS_CAP: int = 80
MAX_DEFECT_MESSAGE_LEN: int = 500  # cap echoed Gemini message (anti prompt-bloat / PII safety)

DETECT_RMBG_DEFECTS_SYSTEM_NAME: str = "DETECT_RMBG_DEFECTS_SYSTEM"
DETECT_RMBG_DEFECTS_DEFAULT_MODEL: str = "gemini-3.5-flash"
DETECT_TEMPERATURE: float = 0.1  # factual localization, reduce false-positive over-flag
DETECT_TIMEOUT_S: float = 90.0
MAX_DETECT_RETRIES: int = 2  # langchain transient-retry (429/5xx); app does 1 parse-retry

# Single source for the 7 RMBG-specific defect categories — drives the response
# Literal + the core's drop-invalid filter. Authoritative names from spec 08
# §categories. Diverges fully from 06/07: these describe the alpha MASK (cutout),
# NOT identity/content. NO category about content (identity/trait/pose/art_style).
RMBG_DEFECT_CATEGORIES: tuple[str, ...] = (
    "background_remnant",
    "foreground_erased",
    "edge_halo",
    "rough_edge",
    "partial_transparency",
    "stray_fragment",
    "other",
)
RmbgDefectCategory = Literal[
    "background_remnant",
    "foreground_erased",
    "edge_halo",
    "rough_edge",
    "partial_transparency",
    "stray_fragment",
    "other",
]
RmbgDefectSeverity = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ─────────────────────────────── request model ─────────────────────────────


class RmbgCrop(_Strict):
    """Lean crop piece — id + media_url + geometry ONLY (no manifest → no
    annotation/objects). For the ORIGINAL group `media_url` is the still-bg piece;
    for `result_crops` it is the RGBA cut-out piece."""

    id: str = Field(min_length=1, max_length=128)
    media_url: str = Field(min_length=1, max_length=2048, pattern=r"^https?://")
    geometry: Geometry


class DetectRmbgDefectsRequest(_Strict):
    """2 crop groups of ONE sheet + optional fast-paths + optional controls.

    `crops` (BEFORE — still background, opaque) is the reference for SUBJECT vs
    BACKGROUND. `result_crops` (AFTER — cut-out, RGBA transparent) is the
    inspection target — every defect box rides this image. `original_sheet_url` /
    `result_sheet_url` are fast-paths (fetch a persisted sheet, skip composing);
    absent → compose from the respective crops. NO swap_objects / swap_model /
    backing_color / identity fields (rmbg only inspects the cutout mask).
    """

    sheet_geometry: SheetGeometry

    # ── Ảnh GỐC (BEFORE — opaque): SUBJECT-vs-BACKGROUND reference ──
    crops: list[RmbgCrop]

    # ── Ảnh KẾT QUẢ (AFTER — RGBA transparent): the inspection target ──
    result_crops: list[RmbgCrop]

    # ── optional fast-paths ──
    original_sheet_url: Optional[str] = Field(default=None, pattern=r"^https?://")
    result_sheet_url: Optional[str] = Field(default=None, pattern=r"^https?://")

    # ── optional controls ──
    severity_threshold: Optional[RmbgDefectSeverity] = None  # default 'low' applied in core
    max_defects: int = Field(default=MAX_DEFECTS_DEFAULT, ge=1, le=MAX_DEFECTS_CAP)
    # AI-usage attribution (Phase 05) — OPTIONAL remix id (billing DISCRIMINATOR).
    # Sync router stamps `AiCallContext(remix_id=remixId)`; the job path injects
    # `ai_context` from the job row instead. `extra="forbid"` → declared, not dropped.
    remixId: Optional[RemixId] = None

    @model_validator(mode="after")
    def _check_business_limits(self) -> "DetectRmbgDefectsRequest":
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

        # ── 2. result_crops: non-empty, ≤ MAX_CROPS, geometry within sheet ──
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

        return self


# ─────────────────────────────── response models ───────────────────────────


class RmbgDefect(BaseModel):
    """One located remove-bg defect region on the RESULT image (px, basis =
    swappedDimensions).

    Reuses the 06 leaf shape (`center`+`radius`+`box`+annotations); the
    differences from `SwapDefect`: `category` ∈ `RmbgDefectCategory` (7 mask
    reasons) and there is NO `object_key` (rmbg has no swap target). `cell` is
    the CROP ordinal the defect center falls into — assigned SERVER-SIDE by
    hit-test (the sheet is composed PLAIN, no badge for Gemini to read).
    """

    center: DefectPoint
    radius: int
    box: Optional[DefectBox] = None
    category: Optional[RmbgDefectCategory] = None
    severity: Optional[RmbgDefectSeverity] = None
    message: Optional[str] = None
    confidence: Optional[float] = None
    cell: Optional[int] = None


class DetectRmbgDefectsData(BaseModel):
    defects: list[RmbgDefect]


class DetectRmbgDefectsMeta(BaseModel):
    cellCount: int
    defectCount: int
    rawDefectCount: Optional[int] = None
    truncated: Optional[bool] = None
    swappedDimensions: SwappedDimensions
    processingTimeMs: Optional[int] = None
    tokenUsage: Optional[int] = None


class DetectRmbgDefectsResponse(BaseModel):
    success: bool
    data: DetectRmbgDefectsData
    meta: Optional[DetectRmbgDefectsMeta] = None
