"""Shared leaf primitives for crop-sheet swap request models.

Extracted from `swap_character_crop_sheet.py` (endpoint 02, removed 2026-06-09)
so the surviving mix-swap + sprite-swap models can import the shared caps +
context shapes without depending on a deleted endpoint module.

Leaf invariant: depends ONLY on `pydantic` (+ no other remix request model) so
it can never form a circular import with mix/sprite/endpoint modules — same role
as `trait_types.py`.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_IMAGE_URL_LEN",
    "MAX_CHARACTER_NAME_LEN",
    "MAX_VISUAL_DESCRIPTION_LEN",
    "MAX_AGE_LEN",
    "MAX_CROP_MANIFEST_BYTES",
    "UnchangedReference",
    "CropSheetCharacterContext",
]


# Schema caps
MAX_IMAGE_URL_LEN = 4096
MAX_CHARACTER_NAME_LEN = 200
MAX_VISUAL_DESCRIPTION_LEN = 4000
MAX_AGE_LEN = 64
# Cap on the pretty-serialized crop_manifest (anti prompt-bloat / annotation
# abuse). Measured on `json.dumps(manifest, ensure_ascii=False, indent=2)` —
# byte-parity with the exact payload `_build_variables` injects into the prompt.
MAX_CROP_MANIFEST_BYTES = 32 * 1024  # 32KB


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UnchangedReference(_Strict):
    """A co-present OTHER character that must NOT be swapped (disambiguation).

    `image_url` = that character's ORIGINAL (pre-swap) variant visual; `name` =
    story character name (NOT a real person — safe to log/render in the prompt).
    """

    image_url: str = Field(min_length=1, max_length=MAX_IMAGE_URL_LEN)
    name: Optional[str] = Field(default=None, max_length=MAX_CHARACTER_NAME_LEN)


class CropSheetCharacterContext(_Strict):
    """Slim character grounding for the crop-sheet full-identity swap prompt.

    Because the reference image is already the fully-swapped visual, the prompt
    only needs lightweight appearance grounding — name + age (body proportions)
    + appearance + visual_description. `basic_info`/`personality` were dropped
    (no signal for a visual swap, and they distracted the model). `extra=forbid`
    rejects the old fields outright so a stale caller fails loudly.

    ALL fields are OPTIONAL: the swap is image-driven (reference carries full
    identity), so textual grounding is a nicety, not a requirement. The mix-swap
    resolver builds name='' for nameless entities (e.g. props) — forcing
    min_length=1 on `name` wrongly rejected the whole batch job.
    """

    name: str = Field(default="", max_length=MAX_CHARACTER_NAME_LEN)
    age: Optional[str] = Field(default=None, max_length=MAX_AGE_LEN)
    appearance: dict[str, Any] = Field(default_factory=dict)
    visual_description: str = Field(default="", max_length=MAX_VISUAL_DESCRIPTION_LEN)
