"""Pydantic models for POST /api/remix/build-crop-sheet (stateless composer).

Spec: ai-storybook-design/api/remix/01-build-crop-sheets.md (composer redesign).
Business limit validators raise `RemixDomainError` so the router maps them to
spec error codes (EMPTY_CROPS, TOO_MANY_CROPS, GEOMETRY_OUT_OF_BOUNDS,
SHEET_TOO_LARGE) at HTTP 400. Schema-level errors (type mismatch, regex fail,
missing field) bubble as FastAPI's default `ValidationError` → HTTP 422.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services.remix.errors import RemixDomainError

__all__ = [
    "DEFAULT_FRAME_GUTTER_COLOR",
    "DEFAULT_FRAME_CELL_STROKE_COLOR",
    "DEFAULT_FRAME_CELL_STROKE_WIDTH",
    "DEFAULT_FRAME_DRAW_ORDINALS",
    "DEFAULT_FRAME_ORDINAL_COLOR",
    "DEFAULT_FRAME_ORDINAL_BG_COLOR",
    "DEFAULT_FRAME_ORDINAL_FONT_PX",
    "MAX_CROPS",
    "MAX_SHEET_DIM",
    "MAX_SHEET_PIXELS",
    "HEX_COLOR_PATTERN",
    "SheetGeometry",
    "Geometry",
    "Crop",
    "FrameStyle",
    "BuildCropSheetRequest",
    "SkippedCrop",
    "BuildCropSheetData",
    "BuildCropSheetMeta",
    "BuildCropSheetResponse",
]


# Mapping constants (spec § Mapping Constants)
# Gutter/background fill for the composed sheet sent to the AI swap model.
# 2026-05-29: flipped magenta (#FF00FF) → white. Trade-off: white loses the
# high-chroma "dead zone" signal and weakens remove-bg (Bria) separation on
# light/pale subjects (white-on-white); cell delineation now leans on the black
# cell stroke. Per-request override still available via FrameStyle.gutter_color.
DEFAULT_FRAME_GUTTER_COLOR = "#FFFFFF"
DEFAULT_FRAME_CELL_STROKE_COLOR = "#000000"
DEFAULT_FRAME_CELL_STROKE_WIDTH = 4
# Ordinal bake (spec § Frame Rendering → Chỉ số ô). Badge số `index+1` vẽ vào
# gutter trên mọi ô. `ordinal_bg_color` giữ hex-only (#RRGGBB) — divergence với
# design (hex/rgba): KISS, nền tối đặc là đủ, tránh parse alpha v1.
DEFAULT_FRAME_DRAW_ORDINALS = True
DEFAULT_FRAME_ORDINAL_COLOR = "#FFFFFF"
DEFAULT_FRAME_ORDINAL_BG_COLOR = "#000000"
DEFAULT_FRAME_ORDINAL_FONT_PX = 28
MAX_CROPS = 64
MAX_SHEET_DIM = 8192
MAX_SHEET_PIXELS = 32_000_000
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class _Strict(BaseModel):
    """Local strict base — `extra='forbid'` for nested request models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SheetGeometry(_Strict):
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_sheet_bounds(self) -> "SheetGeometry":
        # Both dim cap and pixel cap map to SHEET_TOO_LARGE per spec.
        if self.width > MAX_SHEET_DIM or self.height > MAX_SHEET_DIM:
            raise RemixDomainError(
                status=400,
                code="SHEET_TOO_LARGE",
                message=(
                    f"sheet dimension exceeds {MAX_SHEET_DIM}: "
                    f"{self.width}x{self.height}"
                ),
                details={
                    "width": self.width,
                    "height": self.height,
                    "max_dim": MAX_SHEET_DIM,
                },
            )
        if self.width * self.height > MAX_SHEET_PIXELS:
            raise RemixDomainError(
                status=400,
                code="SHEET_TOO_LARGE",
                message=(
                    f"sheet area {self.width}*{self.height}={self.width * self.height} "
                    f"exceeds {MAX_SHEET_PIXELS}"
                ),
                details={
                    "width": self.width,
                    "height": self.height,
                    "max_pixels": MAX_SHEET_PIXELS,
                },
            )
        return self


class Geometry(_Strict):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class Crop(_Strict):
    id: str = Field(min_length=1, max_length=128)
    media_url: str = Field(min_length=1, max_length=2048, pattern=r"^https?://")
    geometry: Geometry
    # Optional pass-through metadata — ONLY the swap endpoint surfaces it (inside
    # crop_manifest, see services/remix/crop_manifest.py). The build composer
    # ignores it entirely (harmless). Open object: caller decides keys
    # (description/expression/...); reserved number/geometry are stripped at
    # manifest build time so they can't shadow the core-injected values.
    annotation: Optional[dict[str, Any]] = None
    # First-class per-cell object roster (variant-qualified mentions) for the mix
    # swap (Phase 2 conform). Bundled WITH the crop (no parallel roster array) so
    # the mix builder renders `crop_manifest[].objects` from it directly; the
    # request validator's `build_crop_manifest` stays roster-free (ignores this).
    # The build composer + sprite path ignore it (default None → no surface change).
    objects: Optional[list[str]] = None


class FrameStyle(_Strict):
    gutter_color: Optional[str] = Field(default=None, pattern=HEX_COLOR_PATTERN)
    cell_stroke_color: Optional[str] = Field(default=None, pattern=HEX_COLOR_PATTERN)
    cell_stroke_width: Optional[int] = Field(default=None, ge=0, le=64)
    # Ordinal bake — omit → service áp DEFAULT_FRAME_ORDINAL_* (draw ON by default).
    # `draw_ordinals=False` phải phân biệt với None (service dùng `is not None`).
    draw_ordinals: Optional[bool] = None
    ordinal_color: Optional[str] = Field(default=None, pattern=HEX_COLOR_PATTERN)
    ordinal_bg_color: Optional[str] = Field(default=None, pattern=HEX_COLOR_PATTERN)
    ordinal_font_px: Optional[int] = Field(default=None, ge=8, le=256)


class BuildCropSheetRequest(_Strict):
    sheet_geometry: SheetGeometry
    crops: list[Crop]
    frame: Optional[FrameStyle] = None
    response_format: Literal["base64", "binary"] = "base64"

    @model_validator(mode="after")
    def _check_business_limits(self) -> "BuildCropSheetRequest":
        n = len(self.crops)
        if n == 0:
            raise RemixDomainError(
                status=400,
                code="EMPTY_CROPS",
                message="crops[] must contain at least 1 item",
            )
        if n > MAX_CROPS:
            raise RemixDomainError(
                status=400,
                code="TOO_MANY_CROPS",
                message=f"crops[] length {n} exceeds maximum {MAX_CROPS}",
                details={"count": n, "max": MAX_CROPS},
            )

        sw = self.sheet_geometry.width
        sh = self.sheet_geometry.height
        for idx, c in enumerate(self.crops):
            g = c.geometry
            if g.x + g.w > sw or g.y + g.h > sh:
                raise RemixDomainError(
                    status=400,
                    code="GEOMETRY_OUT_OF_BOUNDS",
                    message=(
                        f"crop[{idx}] id={c.id} geometry "
                        f"({g.x},{g.y},{g.w}x{g.h}) exceeds sheet {sw}x{sh}"
                    ),
                    details={
                        "index": idx,
                        "id": c.id,
                        "geometry": {"x": g.x, "y": g.y, "w": g.w, "h": g.h},
                        "sheet": {"width": sw, "height": sh},
                    },
                )
        return self


# ---------- Response shapes ----------


class SkippedCrop(BaseModel):
    """Per-crop failure record (partial-success contract)."""

    id: str
    reason: Literal["FETCH_ERROR", "DECODE_ERROR", "OUT_OF_BOUNDS"]
    message: Optional[str] = None


class BuildCropSheetData(BaseModel):
    image_base64: str
    mime_type: Literal["image/png"] = "image/png"
    width: int
    height: int
    composed_count: int
    skipped: list[SkippedCrop]


class BuildCropSheetMeta(BaseModel):
    processingTime: int
    fetchMs: int
    compositeMs: int
    inputBytes: int


class BuildCropSheetResponse(BaseModel):
    success: bool
    data: BuildCropSheetData
    meta: BuildCropSheetMeta
