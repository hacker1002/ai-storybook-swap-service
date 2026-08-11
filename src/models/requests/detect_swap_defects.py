"""Pydantic models for POST /api/remix/detect-swap-defects.

Spec: ai-storybook-design/api/remix/06-detect-swap-defects.md.

Swap defect localization — image-IN / defect-regions-OUT. The request is a
SUPERSET of the sprite-swap body (03): the caller echoes the exact body it sent
to swap (as the reference for "what should the result look like") + the SWAPPED
result pieces to recompose + a few optional controls. `SpriteSheetCrop` /
`SpriteSwapObject` / `SheetGeometry` are reused 1:1 from `swap_sprite_sheet.py`
(DRY — NO crop/object model redefined).

RESULT redesign (commit ba0ae4a): the inspected RESULT is no longer a raw
`swapped_sheet_url` (Gemini-native ~2K) — the caller now sends `result_crops[]`
(the per-cell swapped pieces) and the core RECOMPOSES the result sheet via the
SAME composer as the ORIGINAL. The two sheets are then pixel-aligned
(`swappedDimensions == sheet_geometry`), which the FE overlay relies on.

HTTP-code policy (memory `reference_image_api_validation_http_codes`): every
body/cross-field failure is raised as `RemixDomainError(status=400, ...)` so the
global handler emits the spec envelope at 400 (parity with the swap model's
validator). `max_defects` range is a plain `Field(ge,le)` → 422
`RequestValidationError` → global handler normalizes to 400 VALIDATION_ERROR.

Stateless / advisory — the response carries the located defect regions only; no
persistence, no image edit.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.requests._attribution import RemixId
# Reuse the sprite-swap crop/object/geometry models 1:1 (DRY — caller echoes the
# swap body). `SheetGeometry` already enforces SHEET_TOO_LARGE bounds in its own
# validator, so reusing it gives the dimension caps for free.
from src.models.requests.swap_sprite_sheet import (
    SheetGeometry,
    SpriteSheetCrop,
    SpriteSwapObject,
)
from src.models.requests.build_crop_sheet import MAX_CROPS
from src.services.gemini.payload_budget import (
    MAX_GEMINI_INPUT_IMAGES,
    MAX_SWAP_OBJECTS,
)
from src.services.remix.errors import RemixDomainError

__all__ = [
    # constants
    "MAX_CROPS",
    "MAX_SWAP_OBJECTS",
    "MAX_GEMINI_INPUT_IMAGES",
    "MAX_IMAGE_BYTES",
    "MAX_SWAPPED_SHEET_BYTES",
    "MAX_DEFECTS_DEFAULT",
    "MAX_DEFECTS_CAP",
    "MAX_DEFECT_MESSAGE_LEN",
    "DETECT_SWAP_DEFECTS_SYSTEM_NAME",
    "DETECT_SWAP_DEFECTS_DEFAULT_MODEL",
    "DETECT_TEMPERATURE",
    "DETECT_TIMEOUT_S",
    "MAX_DETECT_RETRIES",
    "SWAP_DEFECT_CATEGORIES",
    "SwapDefectCategory",
    "SwapDefectSeverity",
    # request
    "DetectSwapDefectsRequest",
    # response
    "DefectPoint",
    "DefectBox",
    "SwapDefect",
    "SwappedDimensions",
    "DetectSwapDefectsData",
    "DetectSwapDefectsMeta",
    "DetectSwapDefectsResponse",
]

# ─────────────────────────────── constants ─────────────────────────────────

MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10MB/image (parity sibling endpoints — humans/refs)
# Fetch cap for the `original_sheet_url` FAST-PATH only (the RESULT is now recomposed
# in-process from `result_crops[]`, not fetched). A full ORIGINAL sprite sheet routinely
# exceeds 10MB (a real sheet measured 13.66MB), so the fast-path uses this larger DoS
# bound (~2.3× the observed size) instead of rejecting legit heavy sheets. Crop pieces
# (composer-fetched) keep the 10MB single-image cap. `Image.MAX_IMAGE_PIXELS` still
# guards decompression bombs at decode time.
MAX_SWAPPED_SHEET_BYTES: int = 32 * 1024 * 1024  # 32MB — original_sheet_url fast-path fetch DoS bound
MAX_DEFECTS_DEFAULT: int = 30
MAX_DEFECTS_CAP: int = 80
MAX_DEFECT_MESSAGE_LEN: int = 500  # cap echoed Gemini message (anti prompt-bloat / PII safety)

DETECT_SWAP_DEFECTS_SYSTEM_NAME: str = "DETECT_SWAP_DEFECTS_SYSTEM"
DETECT_SWAP_DEFECTS_DEFAULT_MODEL: str = "gemini-3.5-flash"
DETECT_TEMPERATURE: float = 0.1  # factual localization, reduce false-positive over-flag
DETECT_TIMEOUT_S: float = 90.0
MAX_DETECT_RETRIES: int = 2  # langchain transient-retry (429/5xx); app does 1 parse-retry

# Single source for the 10 defect categories — drives the response Literal +
# the core's drop-invalid filter.
SWAP_DEFECT_CATEGORIES: tuple[str, ...] = (
    "identity_mismatch",
    "trait_leak",
    "cross_contamination",
    "pose_or_composition",
    "art_style_break",
    "ordinal_altered",
    "cell_structure",
    "artifact",
    "not_swapped",
    "other",
)
SwapDefectCategory = Literal[
    "identity_mismatch",
    "trait_leak",
    "cross_contamination",
    "pose_or_composition",
    "art_style_break",
    "ordinal_altered",
    "cell_structure",
    "artifact",
    "not_swapped",
    "other",
]
SwapDefectSeverity = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ─────────────────────────────── request model ─────────────────────────────


class DetectSwapDefectsRequest(_Strict):
    """Superset of the sprite-swap body + result URL + optional controls.

    `crops` / `swap_objects` / `result_crops` reuse the swap models verbatim — the
    caller echoes the body it sent to `03-swap-sprite-sheet` + the per-cell SWAPPED
    pieces. `swap_model` / `swap_temperature` are CONTEXT only (rendered into
    `builder_params`, NEVER used to call Gemini — the detect call hardcodes a
    factual temperature). `original_sheet_url` is a fast-path: present → use as the
    ORIGINAL sheet (skip compose + skip fetching `crops[].media_url`); absent →
    compose from `crops[]`. The RESULT is ALWAYS recomposed from `result_crops[]`
    via the same composer (→ pixel-aligned with the ORIGINAL).
    """

    # ── echo of the swap body (references to judge the result against) ──
    sheet_geometry: SheetGeometry
    crops: list[SpriteSheetCrop]
    swap_objects: list[SpriteSwapObject]

    # ── builder params already used (context only) ──
    # Bounded length even though it's display-only: it IS rendered verbatim into
    # the Gemini `builder_params` text, so cap it (anti prompt-bloat / injection).
    swap_model: Optional[str] = Field(default=None, max_length=120)
    # CONTEXT only — rendered as display text into builder_params, never forwarded
    # to Gemini as the detect call temperature (so no range bound needed).
    swap_temperature: Optional[float] = None

    # ── result to inspect (per-cell SWAPPED pieces → core recomposes the sheet) ──
    # Same shape + parity (geometry/order) as `crops`, so the recomposed RESULT is
    # pixel-aligned with the recomposed ORIGINAL (the compose path — job 11). Each
    # piece is fetched + composited by the SAME composer (SSRF-guarded per piece).
    result_crops: list[SpriteSheetCrop]

    # ── optional fast-path + controls ──
    original_sheet_url: Optional[str] = Field(default=None, pattern=r"^https?://")
    focus_objects: Optional[list[str]] = None
    severity_threshold: Optional[SwapDefectSeverity] = None  # default 'low' applied in core
    max_defects: int = Field(default=MAX_DEFECTS_DEFAULT, ge=1, le=MAX_DEFECTS_CAP)
    # AI-usage attribution (Phase 05) — OPTIONAL remix id (billing DISCRIMINATOR).
    # Sync router stamps `AiCallContext(remix_id=remixId)`; the job path injects
    # `ai_context` from the job row instead. `extra="forbid"` → declared, not dropped.
    remixId: Optional[RemixId] = None

    @model_validator(mode="after")
    def _check_business_limits(self) -> "DetectSwapDefectsRequest":
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
        sw = self.sheet_geometry.width
        sh = self.sheet_geometry.height
        for idx, c in enumerate(self.crops):
            g = c.geometry
            if g.x + g.w > sw or g.y + g.h > sh:
                raise RemixDomainError(
                    status=400, code="GEOMETRY_OUT_OF_BOUNDS",
                    message=(
                        f"crop[{idx}] object_key={c.object_key}/{c.variant_key} "
                        f"geometry ({g.x},{g.y},{g.w}x{g.h}) exceeds sheet {sw}x{sh}"
                    ),
                    details={"index": idx, "object_key": c.object_key},
                )

        # ── 1b. result_crops: non-empty, ≤ MAX_CROPS, geometry within sheet ──
        #     (the SWAPPED pieces the core recomposes into the RESULT sheet).
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
                        f"result_crop[{idx}] object_key={c.object_key}/{c.variant_key} "
                        f"geometry ({g.x},{g.y},{g.w}x{g.h}) exceeds sheet {sw}x{sh}"
                    ),
                    details={"index": idx, "object_key": c.object_key, "field": "result_crops"},
                )

        # ── 2. swap_objects: non-empty, ≤ MAX_SWAP_OBJECTS ──
        objects = self.swap_objects
        if len(objects) == 0:
            raise RemixDomainError(
                status=400, code="EMPTY_SWAP_OBJECTS",
                message="swap_objects[] must contain at least 1 item",
            )
        if len(objects) > MAX_SWAP_OBJECTS:
            raise RemixDomainError(
                status=400, code="TOO_MANY_SWAP_OBJECTS",
                message=(
                    f"swap_objects[] length {len(objects)} exceeds maximum "
                    f"{MAX_SWAP_OBJECTS}"
                ),
                details={"count": len(objects), "max": MAX_SWAP_OBJECTS},
            )

        # ── 3. object_key uniqueness ──
        keys = [o.object_key for o in objects]
        if len(set(keys)) != len(keys):
            seen: set[str] = set()
            dup: Optional[str] = None
            for k in keys:
                if k in seen:
                    dup = k
                    break
                seen.add(k)
            raise RemixDomainError(
                status=400, code="DUPLICATE_OBJECT_KEY",
                message="swap_objects[].object_key must be unique within the request",
                details={"object_key": dup},
            )

        # ── 4. per-object: human URL scheme + trait-type uniqueness ──
        #     (swap_traits min_length=1 + trait Literal already enforced at schema
        #     level by SpriteSwapObject / SwapTrait.)
        for idx, o in enumerate(objects):
            if not (
                o.human_image_url.startswith("http://")
                or o.human_image_url.startswith("https://")
            ):
                raise RemixDomainError(
                    status=400, code="VALIDATION_ERROR",
                    message="human_image_url must start with http(s)://",
                    details={"field": "human_image_url", "index": idx},
                )
            trait_types = [t.type for t in o.swap_traits]
            if len(set(trait_types)) != len(trait_types):
                raise RemixDomainError(
                    status=400, code="INVALID_TRAIT_TYPE",
                    message="swap_traits[].type must be unique within an object",
                    details={"index": idx, "object_key": o.object_key},
                )

        # ── 5. focus_objects ⊆ object_keys ──
        if self.focus_objects is not None:
            valid = set(keys)
            unknown = sorted({k for k in self.focus_objects if k not in valid})
            if unknown:
                raise RemixDomainError(
                    status=400, code="VALIDATION_ERROR",
                    message="focus_objects must be a subset of swap_objects[].object_key",
                    details={"focus_objects": unknown},
                )

        # ── 6. Gemini input-image ceiling: orig + M humans + result ≤ 14 ──
        n_images = 1 + len(objects) + 1
        if n_images > MAX_GEMINI_INPUT_IMAGES:
            raise RemixDomainError(
                status=400, code="TOO_MANY_INPUT_IMAGES",
                message=(
                    f"projected Gemini input images {n_images} "
                    f"(1 orig + {len(objects)} humans + 1 result) exceeds technical "
                    f"ceiling {MAX_GEMINI_INPUT_IMAGES}"
                ),
                details={"count": n_images, "max": MAX_GEMINI_INPUT_IMAGES},
            )

        return self


# ─────────────────────────────── response models ───────────────────────────


class DefectPoint(BaseModel):
    x: int
    y: int


class DefectBox(BaseModel):
    x: int
    y: int
    w: int
    h: int


class SwapDefect(BaseModel):
    """One located defect region on the RESULT image (px, basis = swappedDimensions).

    `center` + `radius` = the minimal enclosing circle of the Gemini box (= half
    the box diagonal). `box` is the raw region. All annotation fields are optional
    (`category` / `severity` / `message` / `confidence` / `cell` / `object_key`).
    """

    center: DefectPoint
    radius: int
    box: Optional[DefectBox] = None
    category: Optional[SwapDefectCategory] = None
    severity: Optional[SwapDefectSeverity] = None
    message: Optional[str] = None
    confidence: Optional[float] = None
    cell: Optional[int] = None
    object_key: Optional[str] = None


class SwappedDimensions(BaseModel):
    width: int
    height: int


class DetectSwapDefectsData(BaseModel):
    defects: list[SwapDefect]


class DetectSwapDefectsMeta(BaseModel):
    cellCount: int
    objectCount: int
    defectCount: int
    rawDefectCount: Optional[int] = None
    truncated: Optional[bool] = None
    swappedDimensions: SwappedDimensions
    processingTimeMs: Optional[int] = None
    tokenUsage: Optional[int] = None


class DetectSwapDefectsResponse(BaseModel):
    success: bool
    data: DetectSwapDefectsData
    meta: Optional[DetectSwapDefectsMeta] = None
