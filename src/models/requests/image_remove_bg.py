"""Core-layer models + constants for the Bria/851-labs remove-background core (P3b).

Ported from `ai-storybook-image-api/src/models/requests/image_remove_bg.py`. In
this service only the CORE contract is exercised — by `routers/retouch/
image_remove_bg.py::image_remove_bg_core` (the `remix_rmbg` job's per-sheet
remove-bg call, `return_bytes=True`) and by `services/replicate_client.run_remove_bg`
(`BRIA_REMOVE_BG_MODEL`). The public HTTP layer models are intentionally NOT ported
(no route mounts them here).
"""

from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

__all__ = [
    "ImageRemoveBgRequest",
    "ImageRemoveBgCoreResult",
    "BRIA_REMOVE_BG_MODEL",
    "REPLICATE_TIMEOUT_S",
]


# Model pinned by owner/name; Replicate resolves latest version server-side.
BRIA_REMOVE_BG_MODEL: str = "bria/remove-background"
REPLICATE_TIMEOUT_S: float = 120.0

# `#RGB` shorthand or `#RRGGBB`. Validated only when backgroundColor is non-null.
HEX_COLOR_REGEX = r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"


# --- Core layer (framework-agnostic, no camelCase alias) ------------------


class ImageRemoveBgRequest(BaseModel):
    """Core-layer request the in-process caller builds directly.

    Three input modes — exactly-one-of `imageUrl | imageBase64 | imageBytes`.
    `imageBytes` (in-process pipeline only) SKIPS the base64 decode + its 10 MB cap
    (trusted internal bytes); mime sniff still applies. `return_bytes=True` → the
    core returns the processed PNG bytes in `image_bytes` and SKIPS Storage upload.
    """

    model_config = ConfigDict(extra="forbid")

    imageUrl: Optional[str] = None
    imageBase64: Optional[str] = None
    # Excluded from JSON serialization — only set by in-process callers.
    imageBytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
    preserveAlpha: bool = True
    backgroundColor: Annotated[
        str, StringConstraints(pattern=HEX_COLOR_REGEX)
    ] | None = None
    return_bytes: bool = False
    # Optional Replicate bg-removal model (public == provider, owner/name). None →
    # BRIA_REMOVE_BG_MODEL default. Both bria + 851-labs ship in v1.
    model: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ImageRemoveBgRequest":
        sources_set = sum(
            1 for v in (self.imageUrl, self.imageBase64, self.imageBytes) if v
        )
        if sources_set != 1:
            raise ValueError(
                "Exactly one of imageUrl, imageBase64, or imageBytes is required"
            )
        return self


class ImageRemoveBgCoreResult(BaseModel):
    """Core-layer result. URL mode populates `imageUrl`/`storagePath`; bytes mode
    populates `image_bytes` (URL fields None)."""

    imageUrl: Optional[str] = None
    storagePath: Optional[str] = None
    mimeType: str = "image/png"
    replicatePredictionId: Optional[str] = None
    backgroundColor: Optional[str] = None
    aiRequestId: Optional[str] = None
    media_url: Optional[str] = None
    # Excluded from JSON serialization — only consumed by in-process callers.
    image_bytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
