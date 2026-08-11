"""Base Pydantic model configuration for ported models.

Ported from `ai-storybook-image-api/src/models/base.py` so the remix job enqueue
models (which subclass `FlexibleModel`) stay byte-identical after the P3b port.
"""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base model with strict configuration."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="forbid",
    )


class FlexibleModel(BaseModel):
    """Base model allowing extra fields (for JSONB flexibility)."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_default=True,
        extra="ignore",
    )
