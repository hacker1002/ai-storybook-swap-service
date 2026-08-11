"""Pydantic models + constants for POST /api/remix/detect-crop-geometry.

Single contract home (KISS): request/response models, endpoint constants, and the
Gemini `assignments` response schema. The model is DB-resolved (`prompt_templates.
model` = bare `gemini-3.5-flash` per seed) — there is NO `modelParams`/allowlist on
this endpoint (it is job-internal + sync-router only), so no provider-prefix
resolver is needed (unlike detect-objects whose seed used `google/...`).

HTTP-code policy (memory `reference_image_api_validation_http_codes`): Pydantic body
errors (shape/length/range, number uniqueness) → 400 via the global handler; cross-
field PRECONDITIONS that need the whole request (geometry within sheet dims,
`target_numbers ⊆ crops`) are checked in the ROUTER → 422.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from src.models.requests._attribution import RemixId

__all__ = [
    # constants
    "MAX_IMAGE_BYTES",
    "MAX_CROPS",
    "MAX_HINT_LEN",
    "DETECT_CROP_GEOMETRY_SYSTEM_NAME",
    "DETECT_CROP_GEOMETRY_DEFAULT_MODEL",
    "DETECT_TEMPERATURE",
    "DETECT_TIMEOUT_S",
    "MAX_DETECT_RETRIES",
    "ASSIGNMENTS_SCHEMA",
    # request
    "CropGeometry",
    "OriginalCrop",
    "SheetDimensions",
    "DetectCropGeometryRequest",
    # response
    "CropBox",
    "CropDetection",
    "DetectCropGeometryData",
    "DetectCropGeometryMeta",
    "DetectCropGeometryResponse",
]

# ─────────────────────────────── constants ─────────────────────────────────

MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10MB/image (parity sibling endpoints)
MAX_CROPS: int = 24
MAX_HINT_LEN: int = 500

DETECT_CROP_GEOMETRY_SYSTEM_NAME: str = "DETECT_CROP_GEOMETRY_SYSTEM"
DETECT_CROP_GEOMETRY_DEFAULT_MODEL: str = "gemini-3.5-flash"
DETECT_TEMPERATURE: float = 0.1  # factual classify, reduce blind assignment
DETECT_TIMEOUT_S: float = 60.0
MAX_DETECT_RETRIES: int = 2  # langchain transient-retry (429/5xx) — app does 1 parse-retry

# Gemini structured-output schema — assignments only (NO coordinates; box comes
# 100% from numpy Step-1). `confidence` optional (model may omit).
ASSIGNMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "frame_index": {"type": "integer"},
                    "number": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": ["frame_index", "number"],
            },
        }
    },
    "required": ["assignments"],
}


# ─────────────────────────────── request models ────────────────────────────


class CropGeometry(BaseModel):
    model_config = {"extra": "forbid"}

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)


class OriginalCrop(BaseModel):
    model_config = {"extra": "forbid"}

    number: int = Field(ge=1)
    geometry: CropGeometry
    recognition_hint: str | None = Field(default=None, max_length=MAX_HINT_LEN)


class SheetDimensions(BaseModel):
    model_config = {"extra": "forbid"}

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class DetectCropGeometryRequest(BaseModel):
    """Detect-crop-geometry request. `swapped_sheet_url` stays required even when the
    in-process job caller passes `swapped_bytes` (the job has the uploaded raw-sheet
    URL on hand) — keeps the contract single-shaped for router + job."""

    model_config = {"extra": "forbid"}

    original_sheet_url: HttpUrl
    swapped_sheet_url: HttpUrl
    crops: list[OriginalCrop] = Field(min_length=1, max_length=MAX_CROPS)
    original_sheet_dimensions: SheetDimensions
    target_numbers: list[int] | None = None
    # AI-usage attribution (Phase 05) — OPTIONAL remix id (billing DISCRIMINATOR).
    # The sync router stamps `AiCallContext(remix_id=remixId)` → Gemini classify cost
    # rolls up to the remix. `extra="forbid"` makes it a declared field, not a drop.
    remixId: RemixId | None = None

    @model_validator(mode="after")
    def _unique_numbers(self) -> "DetectCropGeometryRequest":
        numbers = [c.number for c in self.crops]
        if len(set(numbers)) != len(numbers):
            raise ValueError("crops[].number must be unique")
        return self


# ─────────────────────────────── response models ───────────────────────────


class CropBox(BaseModel):
    x: int
    y: int
    w: int
    h: int


class CropDetection(BaseModel):
    number: int
    box: CropBox  # px on the SWAPPED image — detected inner-of-stroke frame, verbatim (no ratio reshape)
    confidence: float
    source: Literal["semantic", "positional_fallback"]


class DetectCropGeometryData(BaseModel):
    detections: list[CropDetection]


class DetectCropGeometryMeta(BaseModel):
    requestedCount: int | None = None
    detectedCount: int | None = None
    frameCount: int | None = None
    droppedFrames: int | None = None
    notFound: list[int] | None = None
    reorderDetected: bool | None = None
    degraded: bool | None = None
    processingTimeMs: int | None = None
    tokenUsage: int | None = None


class DetectCropGeometryResponse(BaseModel):
    success: bool
    data: DetectCropGeometryData
    meta: DetectCropGeometryMeta | None = None
