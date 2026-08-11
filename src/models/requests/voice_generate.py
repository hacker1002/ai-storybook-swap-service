"""Pydantic models for POST /api/voice/generate-from-prompt.

Contract-parity with spec `ai-storybook-design/api/voice/01-generate-from-prompt.md`.
Response models use camelCase aliases (FE-facing); request keeps snake_case-compatible
field names matching FE request body (description, gender, age, language, ...).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models.base import FlexibleModel

__all__ = [
    "GenerateFromPromptRequest",
    "PreviewCandidate",
    "GenerateFromPromptData",
    "GenerateFromPromptMeta",
    "GenerateFromPromptResponse",
    "DESCRIPTION_MIN_CHARS",
    "DESCRIPTION_MAX_CHARS",
    "SEED_MAX",
]


DESCRIPTION_MIN_CHARS = 20
DESCRIPTION_MAX_CHARS = 1000
# ElevenLabs validates seed as signed int32 (confirmed empirically — rejected
# values > 2^31-1 with `less_than_equal` error). Spec said uint32; reality says int32.
SEED_MAX = 2**31 - 1


class GenerateFromPromptRequest(FlexibleModel):
    """FE request body — constraints mirror test-voice-generate-from-prompt.sh assertions."""

    model_config = ConfigDict(
        str_strip_whitespace=False,  # preserve description whitespace for voice prompt
        validate_default=True,
        extra="ignore",
    )

    description: str = Field(
        min_length=DESCRIPTION_MIN_CHARS,
        max_length=DESCRIPTION_MAX_CHARS,
    )
    gender: Literal[0, 1]
    age: Literal[0, 1, 2]
    language: str = Field(min_length=1, max_length=16)
    accent: str = Field(default="any", min_length=1, max_length=64)
    loudness: float = Field(default=0.5, ge=0.0, le=1.0)
    guidance: float = Field(default=0.75, ge=0.0, le=1.0)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)


class _CamelModel(BaseModel):
    """Response sub-model — serialize with camelCase aliases for FE."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=lambda s: s.split("_")[0]
        + "".join(w.capitalize() for w in s.split("_")[1:]),
    )


class PreviewCandidate(_CamelModel):
    generated_voice_id: str
    audio_base64: str
    media_type: Literal["audio/mpeg"] = "audio/mpeg"
    duration_secs: float


class GenerateFromPromptData(_CamelModel):
    preview_text: str
    previews: list[PreviewCandidate]


class GenerateFromPromptMeta(_CamelModel):
    processing_time_ms: int
    eleven_design_ms: int
    model_id: Literal["eleven_ttv_v3"] = "eleven_ttv_v3"
    seed: int


class GenerateFromPromptResponse(_CamelModel):
    success: bool = True
    data: GenerateFromPromptData
    meta: GenerateFromPromptMeta
