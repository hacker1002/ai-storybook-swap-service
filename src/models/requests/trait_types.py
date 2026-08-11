"""Shared trait-swap primitives for remix swap endpoints.

Leaf module — imports NOTHING from the swap request models, so the sprite-swap
model + trait resolver + prompt builder can import from here without a circular
dependency.

`TRAIT_TYPES` doubles as the canonical sort order for the deterministic
prompt-variable rendering in the service cores.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_TRAIT_DESCRIPTION_LEN",
    "TRAIT_TYPES",
    "TraitType",
    "SwapTrait",
]

MAX_TRAIT_DESCRIPTION_LEN = 2000

# Trait enum + canonical order — used both for schema validation and for the
# deterministic prompt-variable sort in the service cores.
TRAIT_TYPES: tuple[str, ...] = ("face", "facewear", "hair", "skin", "outfit")
TraitType = Literal["face", "facewear", "hair", "skin", "outfit"]


class SwapTrait(BaseModel):
    """A single trait to swap from the reference into the target visual.

    `type` constrained to the canonical enum at schema level (Pydantic Literal
    → 422 → unified VALIDATION_ERROR). Cross-item rules (duplicate type) are
    enforced in each request validator with spec codes. `extra="forbid"` keeps
    callers honest (no `is_enabled` leakage — caller filters upstream).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: TraitType
    description: str = Field(min_length=1, max_length=MAX_TRAIT_DESCRIPTION_LEN)
