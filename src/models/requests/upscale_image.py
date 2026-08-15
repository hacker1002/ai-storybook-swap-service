"""Pydantic models + constants for the upscale core (P3b port).

Ported from `ai-storybook-python-api/src/models/requests/upscale_image.py`. In this
service ONLY the CORE contract (`UpscaleCoreRequest`/`Result`), the grain models,
and the module constants are exercised — by `services/image/upscale_core.py` +
the `remix_upscale` job handler. The public HTTP layer models are kept verbatim
for parity (no route mounts them here).

AI upscaler via Replicate (4 models: xinntao/realesrgan default Anime, real-esrgan,
alexgenovese, recraft-crisp). See CLAUDE.md → upscale-image.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from src.models.requests._attribution import RemixId, SnapshotId
from src.services.image.errors import ImageDomainError
from src.services.resource_persist import SaveResourceDirective

__all__ = [
    "UpscaleImageParams",
    "UpscaleImageModelParams",
    "UpscaleImageModelParamsInner",
    "UpscaleImageData",
    "UpscaleImageMeta",
    "UpscaleImageResponse",
    "UpscaleCoreRequest",
    "UpscaleCoreResult",
    "REAL_ESRGAN_MODEL",
    "REAL_ESRGAN_VERSION",
    "ALEXGENOVESE_UPSCALER_VERSION",
    "RECRAFT_CRISP_UPSCALE_MODEL",
    "RECRAFT_CRISP_UPSCALE_VERSION",
    "XINNTAO_REALESRGAN_MODEL",
    "XINNTAO_REALESRGAN_VERSION",
    "XINNTAO_REALESRGAN_VARIANTS",
    "XINNTAO_REALESRGAN_DEFAULT_VARIANT",
    "REAL_ESRGAN_MAX_INPUT_PIXELS",
    "REAL_ESRGAN_SAFE_LONGEST_EDGE_PX",
    "REPLICATE_TIMEOUT_S",
    "MAX_DECODED_BYTES",
    "INPUT_FETCH_MAX_BYTES",
    "INPUT_FETCH_TIMEOUT_S",
    "OUTPUT_FETCH_MAX_BYTES",
    "OUTPUT_FETCH_TIMEOUT_S",
    "ALLOWED_INPUT_MIMES",
    "TILE_MAX_COUNT",
    "TILE_OVERLAP_INPUT_PX",
    "TILE_CONCURRENCY",
    "TILE_RETRY_MAX_ATTEMPTS",
    "GrainParams",
    "GrainMeta",
    "GRAIN_DEFAULT_AMP",
    "GRAIN_DEFAULT_BLUR",
    "GRAIN_DEFAULT_SEED",
    "GRAIN_AMP_MAX",
    "GRAIN_BLUR_MAX",
    "GRAIN_MAX_PIXELS",
]


# --- Constants (mapping/contract — keep verbatim) -------------------------

REAL_ESRGAN_MODEL: str = "nightmareai/real-esrgan"  # log/trace context only
REAL_ESRGAN_VERSION: str = (
    "nightmareai/real-esrgan:"
    "b3ef194191d13140337468c916c2c5b96dd0cb06dffc032a022a31807f6a5ea8"
)
ALEXGENOVESE_UPSCALER_VERSION: str = (
    "alexgenovese/upscaler:"
    "4f7eb3da655b5182e559d50a0437440f242992d47e5e20bd82829a79dee61ff3"
)
RECRAFT_CRISP_UPSCALE_MODEL: str = "recraft-ai/recraft-crisp-upscale"  # model_id/log only
RECRAFT_CRISP_UPSCALE_VERSION: str = (
    "recraft-ai/recraft-crisp-upscale:"
    "2177c1e3a177f5a76c632e467c32b413e424c23d84e43f7b036a965e305f6557"
)
XINNTAO_REALESRGAN_MODEL: str = "xinntao/realesrgan"  # log/trace context only
XINNTAO_REALESRGAN_VERSION: str = (
    "xinntao/realesrgan:"
    "1b976a4d456ed9e4d1a846597b7614e79eadad3032e9124fa63859db0fd59b56"
)
# Weight-variant enum verbatim from the live model input schema (`version` field).
XINNTAO_REALESRGAN_VARIANTS: tuple[str, ...] = (
    "General - RealESRGANplus",
    "General - v3",
    "Anime - anime6B",
    "AnimeVideo - v3",
)
XINNTAO_REALESRGAN_DEFAULT_VARIANT: str = "Anime - anime6B"
REPLICATE_TIMEOUT_S: float = 180.0
MAX_DECODED_BYTES: int = 10 * 1024 * 1024
INPUT_FETCH_MAX_BYTES: int = 20 * 1024 * 1024
INPUT_FETCH_TIMEOUT_S: float = 15.0
OUTPUT_FETCH_MAX_BYTES: int = 50 * 1024 * 1024
OUTPUT_FETCH_TIMEOUT_S: float = 30.0
# Real-ESRGAN hardware GPU cap; inputs above this are auto-tiled.
REAL_ESRGAN_MAX_INPUT_PIXELS: int = 2_096_704
REAL_ESRGAN_SAFE_LONGEST_EDGE_PX: int = 1448

# Tile mode (auto-triggered when src_px > REAL_ESRGAN_MAX_INPUT_PIXELS).
TILE_MAX_COUNT: int = 6                     # 6 × cap ≈ 12.6MP effective input ceiling
TILE_OVERLAP_INPUT_PX: int = 32             # input-space overlap; × scale = output blend band
TILE_CONCURRENCY: int = 1                   # max concurrent in-flight CREATE calls (not wait/fetch)
TILE_RETRY_MAX_ATTEMPTS: int = 3            # 1 initial + 2 retries on 429

ALLOWED_INPUT_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)

# --- Watercolor grain post-process (2026-06-29) ---------------------------
GRAIN_DEFAULT_AMP: float = 9.0     # amplitude (≈6–9 subtle, higher = grainier)
GRAIN_DEFAULT_BLUR: float = 0.8    # Gaussian softening radius (paper feel)
GRAIN_DEFAULT_SEED: int = 7        # reproducible per (image, seed)
GRAIN_AMP_MAX: float = 50.0        # public body upper bound
GRAIN_BLUR_MAX: float = 5.0        # public body upper bound
GRAIN_MAX_PIXELS: int = 50_000_000


# --- HTTP layer models ----------------------------------------------------


class GrainParams(BaseModel):
    """Watercolor-grain knob — top-level (model-agnostic). Shared by the flat core
    request + the job-10 body. `enabled=false` (or omitting the whole object) →
    skip grain. Bounds enforced here. `seed` omit → default 7 (job 10 adds
    `crop_idx` per crop for per-cell variation)."""

    enabled: bool = False
    amp: float = Field(default=GRAIN_DEFAULT_AMP, ge=0, le=GRAIN_AMP_MAX)
    blur: float = Field(default=GRAIN_DEFAULT_BLUR, ge=0, le=GRAIN_BLUR_MAX)
    seed: int = Field(default=GRAIN_DEFAULT_SEED, ge=0)


class GrainMeta(BaseModel):
    amp: float
    blur: float
    seed: int


class UpscaleImageModelParamsInner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    faceEnhance: Optional[bool] = None


class UpscaleImageModelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = None
    params: Optional[UpscaleImageModelParamsInner] = None


class UpscaleImageParams(BaseModel):
    """Request body. Exactly one of `imageUrl` / `imageBase64` required."""

    model_config = ConfigDict(extra="forbid")

    imageUrl: Optional[HttpUrl] = None
    imageBase64: Optional[str] = None
    scale: float = Field(default=4.0, gt=0, le=10)
    modelParams: Optional[UpscaleImageModelParams] = None
    grain: Optional[GrainParams] = None
    snapshotId: Optional[SnapshotId] = None
    remixId: Optional[RemixId] = None
    saveResource: Optional[SaveResourceDirective] = None

    @model_validator(mode="after")
    def _check_source(self) -> "UpscaleImageParams":
        sources_set = sum(
            1 for v in (self.imageUrl, self.imageBase64) if v is not None
        )
        if sources_set != 1:
            raise ImageDomainError(
                status=422,
                code="INVALID_IMAGE_SOURCE",
                message="Exactly one of imageUrl or imageBase64 is required",
                details={"sourcesProvided": sources_set},
            )
        return self


class UpscaleImageData(BaseModel):
    imageUrl: str
    storagePath: str
    width: int
    height: int
    aiRequestId: str | None = None
    saved: bool | None = None
    snapshotId: str | None = None
    saveError: str | None = None


class UpscaleImageMeta(BaseModel):
    processingTime: Optional[int] = None
    mimeType: Optional[str] = "image/png"
    scale: Optional[float] = None
    sourceType: Optional[str] = None
    tileCount: Optional[int] = None
    replicatePredictionIds: Optional[list[str]] = None
    model: Optional[str] = None
    fixedRatio: Optional[bool] = None
    variant: Optional[str] = None
    grainApplied: Optional[bool] = None
    grain: Optional[GrainMeta] = None


class UpscaleImageResponse(BaseModel):
    success: bool
    data: UpscaleImageData
    meta: Optional[UpscaleImageMeta] = None


# --- Core layer models (framework-agnostic, no camelCase alias) -----------


class UpscaleCoreRequest(BaseModel):
    """Flat resolved request the core consumes. A job builds this directly.

    Three input modes — exactly-one-of `imageUrl | imageBase64 | imageBytes`.
    `imageBytes` (in-process only) skips base64 decode + the 10 MB cap; mime sniff
    still applies. `return_bytes=True` → the core skips Storage upload and populates
    `UpscaleCoreResult.image_bytes`.
    """

    imageUrl: Optional[str] = None
    imageBase64: Optional[str] = None
    imageBytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
    scale: float = 4.0
    faceEnhance: bool = True  # core default ON; callers opt out explicitly
    originName: Optional[str] = None
    return_bytes: bool = False
    model: Optional[str] = None
    grain: Optional[GrainParams] = None

    @model_validator(mode="after")
    def _check_exactly_one_source(self) -> "UpscaleCoreRequest":
        n = sum(
            1 for v in (self.imageUrl, self.imageBase64, self.imageBytes) if v
        )
        if n != 1:
            raise ImageDomainError(
                status=422,
                code="INVALID_IMAGE_SOURCE",
                message=(
                    "Exactly one of imageUrl, imageBase64, or imageBytes is required"
                ),
                details={"sourcesProvided": n},
            )
        return self


class UpscaleCoreResult(BaseModel):
    """URL mode populates `imageUrl`/`storagePath`; bytes mode populates
    `image_bytes` (URL fields None)."""

    imageUrl: Optional[str] = None
    storagePath: Optional[str] = None
    width: int
    height: int
    mimeType: str
    scale: float
    sourceType: Literal["url", "base64", "bytes"]
    tileCount: int
    replicatePredictionIds: list[str]
    predictions: list[tuple[str, Optional[float]]] = []
    fixedRatio: bool = False
    variant: Optional[str] = None
    grainApplied: bool = False
    grain: Optional[GrainParams] = None
    ai_request_id: Optional[str] = None
    image_bytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
