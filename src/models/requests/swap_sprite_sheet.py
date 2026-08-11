"""Pydantic models for POST /api/remix/swap-sprite-sheet.

Spec: ai-storybook-design/api/remix/03-swap-sprite-sheet.md.

Per-object per-trait swap on a sprite sheet. The endpoint composes a flat sprite
sheet from `crops[]` (each cell = ORIGINAL artwork of one `(object_key,
variant_key)`) + N `swap_objects[]` (each = one human `converted_image` + a list
of selected traits) and regenerates the whole sheet with Gemini — every cell whose
`object_key` matches a swap_object gets ONLY its selected traits replaced; cells
with no matching object are left untouched (generic fallback).

Mirrors the sibling mix-swap model (`swap_mix_crop_sheet.py`) but:
  - per-object per-trait (not per-figure full-identity) — `swap_objects[]` carry
    `swap_traits[]` instead of a single `reference_image_url`;
  - input art = ORIGINAL variant (`crops[].media_url`), NOT `visual_swap_url`;
  - cells locate via baked ordinal + crop_manifest only (NO per-cell base
    locator image) → image set = `1 sheet + M human`.

Business validators raise `RemixDomainError` so the app-level handler maps them
to the spec error codes at HTTP 400. Schema-level failures (e.g. bad trait
`type` Literal, `extra=forbid`) bubble through the unified
`RequestValidationError` handler (→ 400 VALIDATION_ERROR).
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.requests._attribution import RemixId
# Reuse Crop/Geometry/SheetGeometry + MAX_CROPS from build_crop_sheet (DRY).
from src.models.requests.build_crop_sheet import (
    MAX_CROPS,
    Crop,
    Geometry,
    SheetGeometry,
)
# Reuse the slim grounding model + the URL/name caps from the shared leaf (one
# shape, no redefine — same convention as the mix model).
from src.models.requests.crop_sheet_shared import (
    MAX_CHARACTER_NAME_LEN,
    MAX_CROP_MANIFEST_BYTES,
    MAX_IMAGE_URL_LEN,
)
# Reuse the canonical trait enum + per-trait shape (shared leaf module).
from src.models.requests.trait_types import (
    TRAIT_TYPES,
    SwapTrait,
)
# Sprite-sheet caps live in gemini_payload_budget (parity with how the mix model
# imports MAX_SWAP_TARGETS from there).
from src.services.gemini.payload_budget import (
    MAX_GEMINI_INPUT_IMAGES,
    MAX_SWAP_OBJECTS,
)
from src.services.remix.crop_manifest import build_crop_manifest
from src.services.remix.errors import RemixDomainError

__all__ = [
    "MAX_SWAP_OBJECTS",
    "MAX_GEMINI_INPUT_IMAGES",
    "MAX_OBJECT_KEY_LEN",
    "MAX_VARIANT_KEY_LEN",
    "MAX_HUMAN_DESCRIPTION_LEN",
    "SpriteSwapTrait",
    "SpriteObjectContext",
    "SpriteSwapObject",
    "SpriteSheetCrop",
    "SwapSpriteSheetCoreRequest",
    "SwapSpriteSheetCoreResultData",
    "SpriteSheetDimensionsMeta",
    "SpriteGeminiPayloadBytesMeta",
    "SwapSpriteSheetMeta",
    "SwapSpriteSheetResponse",
    # Re-exports for caller convenience.
    "Geometry",
    "SheetGeometry",
    "TRAIT_TYPES",
    "MAX_CROPS",
]


# Schema caps
MAX_OBJECT_KEY_LEN = 200
MAX_VARIANT_KEY_LEN = 200
MAX_HUMAN_DESCRIPTION_LEN = 4000
MAX_VISUAL_DESCRIPTION_LEN = 4000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# `SwapTrait` (trait_types.py) is reused verbatim for the per-trait shape;
# `SpriteSwapTrait` is an alias kept for spec/contract symmetry.
SpriteSwapTrait = SwapTrait


class SpriteObjectContext(_Strict):
    """Character grounding for one swap object (applies to all its variant cells).

    Full shape per spec — name + basic_info + personality + appearance +
    visual_description. All sub-dicts accept empty payloads; the caller decides
    what to surface. `extra=forbid` rejects stray fields so a stale caller fails
    loudly through the unified validation handler.
    """

    name: str = Field(min_length=1, max_length=MAX_CHARACTER_NAME_LEN)
    basic_info: dict[str, Any] = Field(default_factory=dict)
    personality: dict[str, Any] = Field(default_factory=dict)
    appearance: dict[str, Any] = Field(default_factory=dict)
    visual_description: str = Field(default="", max_length=MAX_VISUAL_DESCRIPTION_LEN)


class SpriteSwapObject(_Strict):
    """One character (object) — one human + one trait-set applied to EVERY cell
    sharing its `object_key`.

    `object_key` is an opaque entity key (soft ref to remix.characters[].key) —
    UNIQUE within the request; used to map cell→object and as the (non-PII) join
    key in `meta.swappedObjects` / error `details`. `human_image_url` =
    `converted_image` (the appearance source). `swap_traits[]` are ONLY the
    enabled traits (caller filters upstream). `human_image_index` is core-set
    during the single-source ordered-images pass — NEVER accepted from the body.
    """

    object_key: str = Field(min_length=1, max_length=MAX_OBJECT_KEY_LEN)
    human_image_url: str = Field(min_length=1, max_length=MAX_IMAGE_URL_LEN)
    # OPTIONAL — the sprite-swap prompt/core is image-driven (human reference image
    # + per-trait descriptions); the 2026-06-08 prompt redesign dropped all
    # human/character grounding TEXT, so this is unused here. Kept on the model for
    # contract symmetry, but a visual profile lacking `description` resolves to ''
    # upstream — forcing min_length=1 wrongly rejected the whole job.
    human_description: str = Field(default="", max_length=MAX_HUMAN_DESCRIPTION_LEN)
    swap_traits: list[SpriteSwapTrait] = Field(min_length=1)
    object_context: SpriteObjectContext
    # Core-set (1-based position in ordered_images). Default None; body must NOT
    # provide it (extra=forbid would reject a stray key anyway — kept explicit).
    human_image_index: Optional[int] = Field(default=None, exclude=True)


class SpriteSheetCrop(_Strict):
    """One cell = ORIGINAL artwork of exactly one `(object_key, variant_key)`.

    `media_url` = the ORIGINAL variant artwork (NOT `visual_swap_url` — avoids
    re-swap compounding drift). `object_key` matches a `SpriteSwapObject` (cell
    swapped) or none (cell kept untouched — generic fallback). `variant_key` is
    an opaque echo. `annotation` (optional) is folded into the crop_manifest.
    """

    type: Literal["character", "prop"]
    object_key: str = Field(min_length=1, max_length=MAX_OBJECT_KEY_LEN)
    variant_key: str = Field(min_length=1, max_length=MAX_VARIANT_KEY_LEN)
    media_url: str = Field(
        min_length=1, max_length=MAX_IMAGE_URL_LEN, pattern=r"^https?://"
    )
    geometry: Geometry
    annotation: Optional[dict[str, Any]] = None


class SwapSpriteSheetCoreRequest(_Strict):
    """Core request for `run_swap_sprite_sheet`.

    `return_bytes` is in-process pipeline ONLY — when True the core skips Storage
    upload of the final sheet and populates `SwapSpriteSheetCoreResult.image_bytes`
    with raw PNG bytes (image_url = None). NEVER set from HTTP body (router forces
    field → False); used by job 02 to cut in-process without an upload→download
    roundtrip. `composed_sheet_url` is independent of `return_bytes`.
    """

    sheet_geometry: SheetGeometry
    crops: list[SpriteSheetCrop]
    swap_objects: list[SpriteSwapObject]
    return_composed_sheet: bool = False
    return_bytes: bool = False
    # Phase 02 (model_params wiring): optional per-job model knobs. `model` is the
    # PUBLIC allowlist id (e.g. "google/nano-banana-pro"); the core resolves it to
    # the provider Gemini id internally (`_PUBLIC_TO_GEMINI`). Both None → core
    # hardcoded defaults (GEMINI_MODEL_ID / GEMINI_TEMPERATURE) → behavior parity.
    model: Optional[str] = None
    # Bounded at the schema layer so the PUBLIC sync endpoint (binds this model
    # directly) cannot forward an out-of-range temperature raw to Gemini — the
    # registry clamp only guards the job path. Out-of-range → 400 VALIDATION_ERROR.
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    # AI-usage attribution (Phase 05) — OPTIONAL remix id (the billing DISCRIMINATOR).
    # The sync router stamps `AiCallContext(remix_id=remixId)`; `_Strict`
    # (`extra="forbid"`) makes it a required field to accept, not a silent drop. The
    # job path (jobs 02/05) leaves this None and injects `ai_context` from the job row.
    remixId: Optional[RemixId] = None

    @model_validator(mode="after")
    def _check_business_limits(self) -> "SwapSpriteSheetCoreRequest":
        # ── 1. crops ─────────────────────────────────────────────────────
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
                        f"crop[{idx}] object_key={c.object_key}/{c.variant_key} "
                        f"geometry ({g.x},{g.y},{g.w}x{g.h}) exceeds sheet {sw}x{sh}"
                    ),
                    details={
                        "index": idx,
                        "object_key": c.object_key,
                        "variant_key": c.variant_key,
                        "geometry": {"x": g.x, "y": g.y, "w": g.w, "h": g.h},
                        "sheet": {"width": sw, "height": sh},
                    },
                )

        # ── 2. crop_manifest byte cap (shared builder + serialize the prompt
        #       uses → measures the real payload) ───────────────────────────
        manifest = build_crop_manifest(sprite_crops_as_base(self.crops))
        manifest_bytes = len(
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        )
        if manifest_bytes > MAX_CROP_MANIFEST_BYTES:
            raise RemixDomainError(
                status=400,
                code="CROP_MANIFEST_TOO_LARGE",
                message=(
                    f"crop_manifest {manifest_bytes} bytes exceeds "
                    f"{MAX_CROP_MANIFEST_BYTES}"
                ),
                details={"bytes": manifest_bytes, "max": MAX_CROP_MANIFEST_BYTES},
            )

        # ── 3. swap_objects count ────────────────────────────────────────
        objects = self.swap_objects
        if len(objects) == 0:
            raise RemixDomainError(
                status=400,
                code="EMPTY_SWAP_OBJECTS",
                message="swap_objects[] must contain at least 1 item",
            )
        if len(objects) > MAX_SWAP_OBJECTS:
            raise RemixDomainError(
                status=400,
                code="TOO_MANY_SWAP_OBJECTS",
                message=(
                    f"swap_objects[] length {len(objects)} exceeds maximum "
                    f"{MAX_SWAP_OBJECTS}"
                ),
                details={"count": len(objects), "max": MAX_SWAP_OBJECTS},
            )

        # ── 4. object_key uniqueness ─────────────────────────────────────
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
                status=400,
                code="DUPLICATE_OBJECT_KEY",
                message="swap_objects[].object_key must be unique within the request",
                details={"object_key": dup},
            )

        # ── 5. per-object URL scheme + trait rules ───────────────────────
        for idx, o in enumerate(objects):
            if not (
                o.human_image_url.startswith("http://")
                or o.human_image_url.startswith("https://")
            ):
                raise RemixDomainError(
                    status=400,
                    code="VALIDATION_ERROR",
                    message="human_image_url must start with http(s)://",
                    details={
                        "field": "human_image_url",
                        "index": idx,
                        "object_key": o.object_key,
                    },
                )
            if len(o.swap_traits) == 0:
                raise RemixDomainError(
                    status=400,
                    code="EMPTY_SWAP_TRAITS",
                    message="each swap_objects[].swap_traits[] must be non-empty",
                    details={"index": idx, "object_key": o.object_key},
                )
            trait_types = [t.type for t in o.swap_traits]
            if len(set(trait_types)) != len(trait_types):
                raise RemixDomainError(
                    status=400,
                    code="INVALID_TRAIT_TYPE",
                    message="swap_traits[].type must be unique within an object",
                    details={"index": idx, "object_key": o.object_key},
                )

        # ── 6. crop object_key scheme (non-empty already by Field) ────────
        #       (cell with no matching swap_object is a valid generic fallback —
        #       no error here.)

        # ── 7. Gemini input-images hard technical ceiling ────────────────
        #       1 sheet + M human ≤ 14. Lighter than 04 (no per-cell locator).
        n_images = 1 + len(objects)
        if n_images > MAX_GEMINI_INPUT_IMAGES:
            raise RemixDomainError(
                status=400,
                code="TOO_MANY_INPUT_IMAGES",
                message=(
                    f"projected Gemini input images {n_images} "
                    f"(1 + {len(objects)} humans) exceeds technical ceiling "
                    f"{MAX_GEMINI_INPUT_IMAGES}"
                ),
                details={"count": n_images, "max": MAX_GEMINI_INPUT_IMAGES},
            )

        return self


# ---------- crop_manifest projection ----------


def sprite_crops_as_base(crops: list["SpriteSheetCrop"]) -> list[Crop]:
    """Project sprite crops → base `Crop` objects the shared composer +
    `build_crop_manifest` consume.

    `id` is the variant-qualified token `{object_key}/{variant_key}`. The
    composer bakes the ordinal badge in `crops[]` order (so manifest `number` =
    bake position — OQ#10). `object_key`/`variant_key` are folded into each
    annotation so the model can join cell→object by key in the manifest (reserved
    `number`/`geometry` keys stay core-owned — `build_crop_manifest` strips them).
    """
    out: list[Crop] = []
    for c in crops:
        ann: dict[str, Any] = dict(c.annotation or {})
        ann.setdefault("object_key", c.object_key)
        ann.setdefault("variant_key", c.variant_key)
        ann.setdefault("type", c.type)
        out.append(
            Crop(
                id=f"{c.object_key}/{c.variant_key}",
                media_url=c.media_url,
                geometry=c.geometry,
                annotation=ann,
            )
        )
    return out


# ---------- Response shapes ----------


class SwapSpriteSheetCoreResultData(BaseModel):
    image_url: str
    width: int
    height: int
    token_usage: Optional[int] = None
    composed_sheet_url: Optional[str] = None
    # AI-usage contract (Phase 05): `ai_service_logs.id` of the Gemini swap call —
    # returned for contract uniformity across all 24 sync image-gen endpoints. The
    # sync body now carries an OPTIONAL `remixId` → when present the log row is
    # attributed to the remix (else unattributed; remix billing also flows through the
    # JOB path, jobs 02/05).
    aiRequestId: Optional[str] = None


class SpriteSheetDimensionsMeta(BaseModel):
    width: int
    height: int


class SpriteGeminiPayloadBytesMeta(BaseModel):
    sheet: int
    humans: int  # total raw bytes of ALL M human reference images


class SwapSpriteSheetMeta(BaseModel):
    processingTime: Optional[int] = None
    composeMs: Optional[int] = None
    geminiMs: Optional[int] = None
    uploadMs: Optional[int] = None
    tokenUsage: Optional[int] = None
    sheetDimensions: Optional[SpriteSheetDimensionsMeta] = None
    geminiPayloadBytes: Optional[SpriteGeminiPayloadBytesMeta] = None
    objectCount: Optional[int] = None
    cellCount: Optional[int] = None
    swappedObjects: Optional[list[str]] = None  # object_key only (non-PII)


class SwapSpriteSheetResponse(BaseModel):
    success: bool
    data: SwapSpriteSheetCoreResultData
    meta: SwapSpriteSheetMeta
