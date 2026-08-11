"""Pydantic request/response models for POST /api/text/narrate-script.

Single-turn TTS-with-timestamps shape per spec
`ai-storybook-design/api/text-generation/02-narrate-script.md`.
Wire format: camelCase (FE-facing) via Field(alias=...) + populate_by_name=True.
Python field names stay snake_case.
"""

from typing import Any, Literal, Optional

from pydantic import ConfigDict, Field

from src.models.base import FlexibleModel
from src.models.requests._attribution import BookId, SnapshotId
from src.services.resource_persist import SaveResourceDirective

__all__ = [
    "NarrateScriptRequest",
    "NarrateScriptSettings",
    "NarrateScriptResponse",
    "NarrateScriptData",
    "NarrateScriptMeta",
    "WordTimingResponse",
    "SCRIPT_MAX_CHARS",
    "SEED_MAX",
]


SCRIPT_MAX_CHARS = 3000
# ElevenLabs validates seed as signed int32 (same as voice-design). Keep parity.
SEED_MAX = 2**31 - 1


class NarrateScriptSettings(FlexibleModel):
    """Voice generation tuning — all fields optional with spec defaults.

    `seed` lives here so everything that affects generation/path-key hashing is
    in one object.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_default=True,
        extra="ignore",
    )

    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(
        default=0.75, ge=0.0, le=1.0, alias="similarityBoost"
    )
    style: float = Field(default=0.0, ge=0.0, le=1.0)
    speed: float = Field(default=1.0, ge=0.7, le=1.25)
    seed: Optional[int] = Field(default=None, ge=0, le=SEED_MAX)


class NarrateScriptRequest(FlexibleModel):
    """FE request body — matches spec schema."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=False,  # preserve script whitespace / newlines
        validate_default=True,
        extra="ignore",
    )

    script: str = Field(min_length=1, max_length=SCRIPT_MAX_CHARS)
    model_id: Literal["eleven_v3"] = Field(default="eleven_v3", alias="modelId")
    settings: Optional[NarrateScriptSettings] = None
    output_format: Literal[
        "mp3_44100_128",
        "mp3_44100_192",
        "mp3_22050_32",
        "pcm_44100",
        "pcm_16000",
    ] = Field(default="mp3_44100_128", alias="outputFormat")
    # AI-usage attribution (Phase 05) — DUAL: `snapshotId` for spread narration
    # (book-scoped, resolved to book_id at write time) vs `bookId` for voice-config
    # PREVIEW (book-level, no snapshot). Router branches book_id (wins) ⊕ snapshot_id.
    # Both OPTIONAL, attribution-only. `extra="ignore"` means an undeclared field is
    # dropped, so they MUST be declared here to be captured.
    snapshot_id: SnapshotId | None = Field(default=None, alias="snapshotId")
    book_id: BookId | None = Field(default=None, alias="bookId")
    # Opt-in auto-persist (save-generated-resource). type=textbox_audio_chunk;
    # absent ⇒ no-op ⇒ backward compatible.
    save_resource: SaveResourceDirective | None = Field(
        default=None, alias="saveResource"
    )


# ─── Response models ─────────────────────────────────────────────────────────


class WordTimingResponse(FlexibleModel):
    """Per-word timing. charStart/charEnd offsets reference response.data.text
    (which has audio tags stripped)."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=False,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    text: str
    start_ms: int = Field(alias="startMs")
    end_ms: int = Field(alias="endMs")
    char_start: int = Field(alias="charStart")
    char_end: int = Field(alias="charEnd")


class NarrateScriptData(FlexibleModel):
    """Single-turn flat response data block.

    `text` is the user-facing text with audio tags `[...]` stripped — FE renders
    this directly. ElevenLabs still receives the raw text-with-tags upstream
    (prosody preserved). `words[]` charStart/charEnd offsets index into `text`.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    audio_url: str = Field(alias="audioUrl")
    duration_ms: int = Field(alias="durationMs")
    voice_id: str = Field(alias="voiceId")
    text: str
    words: list[WordTimingResponse]
    # Raw passthrough of ElevenLabs `alignment` object. Clients persist this if
    # they want to recompute word timing later without re-billing. NOT a stable
    # contract — shape follows upstream.
    raw_alignment: dict[str, Any] = Field(alias="rawAlignment")
    # save-generated-resource result (additive; None when no saveResource sent).
    saved: Optional[bool] = None
    snapshot_id: Optional[str] = Field(default=None, alias="snapshotId")
    save_error: Optional[str] = Field(default=None, alias="saveError")


class NarrateScriptMeta(FlexibleModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    processing_time_ms: int = Field(alias="processingTimeMs")
    eleven_call_ms: int = Field(alias="elevenCallMs")
    upload_ms: int = Field(alias="uploadMs")
    model_id: str = Field(alias="modelId")
    path_key: str = Field(alias="pathKey")
    char_count: int = Field(alias="charCount")
    cost_estimate: Optional[float] = Field(default=None, alias="costEstimate")
    alignment_fallback: Optional[bool] = Field(
        default=None, alias="alignmentFallback"
    )
    # "with_timestamps" when upstream returned alignment; "fallback_base" when
    # we synthesized an empty shell (upstream omitted alignment, or base TTS
    # fallback path). Lets clients decide whether to trust raw_alignment.
    raw_alignment_source: Literal["with_timestamps", "fallback_base"] = Field(
        alias="rawAlignmentSource"
    )


class NarrateScriptResponse(FlexibleModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    success: bool
    data: NarrateScriptData
    meta: NarrateScriptMeta
