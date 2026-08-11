"""Pydantic models for POST /api/remix/swap-mix-crop-sheet.

Spec: ai-storybook-design/api/remix/04-swap-mix-crop-sheet.md (⚡rev6
2026-06-11 — variant-sheet input).

Multi-target full-identity crop-sheet swap (N ≤ 10; N=1 is the degenerate
single-target case). Each `SwapTarget` carries its OWN new identity
(`reference_image_url`) + a locator (`target_base_image_url`); ⚡rev6 the core
composes the N old/new images into 2 MIRRORED variant sheets, so the Gemini
input is a fixed 3 images regardless of N. `unchanged_references[]` was REMOVED
from the contract (objects outside `swap_targets` stay untouched by default —
the field is now rejected as an unknown extra). The slim grounding model
(`CropSheetCharacterContext`) is IMPORTED from the shared leaf
(`crop_sheet_shared`) — one shape, no drift.

Business validators raise `RemixDomainError` so the app-level handler maps them
to the spec error codes at HTTP 400. Schema-level failures (e.g. `extra=forbid`
on a stale `unchanged_references`) bubble through the unified
`RequestValidationError` handler (→ 400 VALIDATION_ERROR).
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.requests._attribution import RemixId
from src.services.resource_persist import SaveResourceDirective
# Reuse Crop/Geometry/SheetGeometry + MAX_CROPS from build_crop_sheet (DRY) —
# identical Crop shape as 02.
from src.models.requests.build_crop_sheet import (
    MAX_CROPS,
    Crop,
    Geometry,
    SheetGeometry,
)

# IMPORT the slim grounding model from the shared leaf (one shape, no
# redefine). Also reuse the URL/name caps.
from src.models.requests.crop_sheet_shared import (
    MAX_CHARACTER_NAME_LEN,
    MAX_CROP_MANIFEST_BYTES,
    MAX_IMAGE_URL_LEN,
    CropSheetCharacterContext,
)

# Multi-target cap lives in gemini_payload_budget (⚡rev6 = 10; the old
# MAX_TOTAL_SUBJECTS / MAX_GEMINI_INPUT_IMAGES / MAX_UNCHANGED_REFERENCES
# checks are gone — input is a fixed 3 images regardless of N).
from src.services.gemini.payload_budget import MAX_SWAP_TARGETS
from src.services.remix.crop_manifest import build_crop_manifest
from src.services.remix.errors import RemixDomainError

__all__ = [
    "MAX_SWAP_TARGETS",
    "SwapTarget",
    "SwapMixSheetCoreRequest",
    "SwapMixSheetCoreResultData",
    "VariantSheetUrls",
    "MixSheetDimensionsMeta",
    "MixGeminiPayloadBytesMeta",
    "MixSkippedReferenceMeta",
    "SwapMixCropSheetMeta",
    "SwapMixCropSheetResponse",
    # Re-exports for caller convenience.
    "Crop",
    "Geometry",
    "SheetGeometry",
    "CropSheetCharacterContext",
    "MAX_CROPS",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SwapTarget(_Strict):
    """One OBJECT to swap in the mix lineup — character OR prop (item).

    The endpoint is generic over object type: it only ever sees "a target with a
    reference identity to apply". `key` is an opaque entity key (soft ref to
    remix.characters[].key / props[].key) — UNIQUE within the request; used for
    label/manifest/image_guide and error `details` (it is NOT PII).
    `reference_image_url` is that target's ALREADY-swapped variant visual (the FULL
    new identity, char via pipeline 03 / prop via its own swap pipeline).
    `target_base_image_url` is the ORIGINAL (pre-swap) visual — the LOCATOR that
    pins which figure in the sheet is this target. Optional in the schema but
    runtime-fatal when N≥2 (see core). `object_context` is the slim appearance
    grounding (reuses the 02 `CropSheetCharacterContext` shape — `age` is simply
    null/empty for non-character objects like props).
    """

    key: str = Field(min_length=1, max_length=MAX_CHARACTER_NAME_LEN)
    reference_image_url: str = Field(min_length=1, max_length=MAX_IMAGE_URL_LEN)
    target_base_image_url: Optional[str] = Field(
        default=None, max_length=MAX_IMAGE_URL_LEN
    )
    object_context: CropSheetCharacterContext


class SwapMixSheetCoreRequest(_Strict):
    """Core request for `run_swap_mix_sheet`.

    `return_bytes` is in-process pipeline only — when True, core skips Storage
    upload of the final swap sheet and populates `SwapMixSheetCoreResult.image_bytes`
    with raw PNG bytes (image_url = None). NEVER set from HTTP body (router omits
    field → default False); used by post-swap pipeline to avoid orphan upload +
    fetch roundtrip when sheet > 10 MB (parity with image_remove_bg / upscale
    bytes-mode, commit c92c87e).
    """

    sheet_geometry: SheetGeometry
    crops: list[Crop]
    swap_targets: list[SwapTarget]
    return_composed_sheet: bool = False
    return_bytes: bool = False
    # Phase 02 (model_params wiring): optional per-job model knobs. `model` is the
    # PUBLIC allowlist id; the core resolves it to the provider Gemini id
    # internally (`_PUBLIC_TO_GEMINI`). Both None → core hardcoded defaults
    # (GEMINI_MODEL_ID / GEMINI_TEMPERATURE 0.25) → behavior parity.
    model: Optional[str] = None
    # Bounded at the schema layer so the PUBLIC sync endpoint (binds this model
    # directly) cannot forward an out-of-range temperature raw to Gemini — the
    # registry clamp only guards the job path. Out-of-range → 400 VALIDATION_ERROR.
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    # AI-usage attribution (Phase 05) — OPTIONAL remix id (the billing DISCRIMINATOR).
    # The sync router stamps `AiCallContext(remix_id=remixId)`; `_Strict`
    # (`extra="forbid"`) makes it a required field to accept, not a silent drop. The
    # job path (jobs 04/…) leaves this None and injects `ai_context` from the job row.
    remixId: Optional[RemixId] = None
    # Opt-in auto-persist (save-generated-resource). Remix-context image edit reuses
    # type=image_version, path-routed to Backend B (root table:remixes/col:illustration
    # → prepend Illustration Entry into remixes.illustration). Absent ⇒ no-op. The job
    # path leaves this None (never persists via this seam).
    save_resource: Optional[SaveResourceDirective] = None

    @model_validator(mode="after")
    def _check_business_limits(self) -> "SwapMixSheetCoreRequest":
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

        # ── 2. crop_manifest byte cap (same builder + serialize the prompt
        #       uses, so this measures the real payload) ───────────────────
        manifest = build_crop_manifest(self.crops)
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

        # ── 3. swap_targets count ────────────────────────────────────────
        targets = self.swap_targets
        if len(targets) == 0:
            raise RemixDomainError(
                status=400,
                code="EMPTY_SWAP_TARGETS",
                message="swap_targets[] must contain at least 1 item",
            )
        if len(targets) > MAX_SWAP_TARGETS:
            raise RemixDomainError(
                status=400,
                code="TOO_MANY_SWAP_TARGETS",
                message=(
                    f"swap_targets[] length {len(targets)} exceeds maximum "
                    f"{MAX_SWAP_TARGETS}"
                ),
                details={"count": len(targets), "max": MAX_SWAP_TARGETS},
            )

        # ── 4. key uniqueness ────────────────────────────────────────────
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
                status=400,
                code="DUPLICATE_TARGET_KEY",
                message="swap_targets[].key must be unique within the request",
                details={"key": dup},
            )

        # ── 5. per-target URL schemes ────────────────────────────────────
        for idx, t in enumerate(targets):
            if not (
                t.reference_image_url.startswith("http://")
                or t.reference_image_url.startswith("https://")
            ):
                raise RemixDomainError(
                    status=400,
                    code="VALIDATION_ERROR",
                    message="reference_image_url must start with http(s)://",
                    details={
                        "field": "reference_image_url",
                        "index": idx,
                        "target_key": t.key,
                    },
                )
            if t.target_base_image_url is not None and not (
                t.target_base_image_url.startswith("http://")
                or t.target_base_image_url.startswith("https://")
            ):
                raise RemixDomainError(
                    status=400,
                    code="VALIDATION_ERROR",
                    message="target_base_image_url must start with http(s)://",
                    details={
                        "field": "target_base_image_url",
                        "index": idx,
                        "target_key": t.key,
                    },
                )

        # ⚡rev6 — the old blocks 6/7/8 (unchanged_references / TOO_MANY_SUBJECTS
        # / TOO_MANY_INPUT_IMAGES) are GONE: `unchanged_references` left the
        # contract (now an extra=forbid reject) and the Gemini input is a fixed
        # 3 images regardless of N, so neither ceiling can be exceeded.
        return self


# ---------- Response shapes ----------


class VariantSheetUrls(BaseModel):
    """⚡rev6 — debug URLs of the 2 variant sheets (only when
    `return_composed_sheet=true`). `old` is absent for the N=1 degenerate case
    without a target_base."""

    old: Optional[str] = None
    new: str


class SwapMixSheetCoreResultData(BaseModel):
    image_url: str
    width: int
    height: int
    token_usage: Optional[int] = None
    composed_sheet_url: Optional[str] = None
    variant_sheet_urls: Optional[VariantSheetUrls] = None  # ⚡rev6
    # AI-usage contract (Phase 05): `ai_service_logs.id` of the Gemini swap call —
    # returned for contract uniformity across all 24 sync image-gen endpoints. The
    # sync body now carries an OPTIONAL `remixId` → when present the log row is
    # attributed to the remix (else unattributed; remix billing also flows through the
    # JOB path, jobs 04/…).
    aiRequestId: Optional[str] = None
    # save-generated-resource result (additive; None when no saveResource sent).
    # Backend B (remix whole-column) → snapshotId always None.
    saved: Optional[bool] = None
    snapshotId: Optional[str] = None
    saveError: Optional[str] = None


class MixSheetDimensionsMeta(BaseModel):
    width: int
    height: int


class MixGeminiPayloadBytesMeta(BaseModel):
    """⚡rev6 — per-image payload observability (spec §Result):
    `{ sheet, variant_old?, variant_new }`."""

    sheet: int
    variant_old: Optional[int] = None  # absent when the old sheet was skipped (N=1)
    variant_new: int


class MixSkippedReferenceMeta(BaseModel):
    kind: str  # ⚡rev6 — only 'target_base' (N=1 non-fatal skip)
    target_key: Optional[str] = None
    reason: str  # 'FETCH_ERROR' | 'DECODE_ERROR'


class SwapMixCropSheetMeta(BaseModel):
    processingTime: Optional[int] = None
    composeMs: Optional[int] = None
    geminiMs: Optional[int] = None
    uploadMs: Optional[int] = None
    tokenUsage: Optional[int] = None
    sheetDimensions: Optional[MixSheetDimensionsMeta] = None
    geminiPayloadBytes: Optional[MixGeminiPayloadBytesMeta] = None
    targetCount: Optional[int] = None
    targetsWithBase: Optional[int] = None
    skippedReferences: Optional[list[MixSkippedReferenceMeta]] = None


class SwapMixCropSheetResponse(BaseModel):
    success: bool
    data: SwapMixSheetCoreResultData
    meta: SwapMixCropSheetMeta
