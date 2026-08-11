"""Pydantic request/response models for POST /api/text/combine-audio-chunks.

Stateless ffmpeg-concat shape per spec
`ai-storybook-design/api/text-generation/03-combine-audio-chunks.md`.
Wire format: camelCase (FE-facing) via Field(alias=...) + populate_by_name=True.
Python field names stay snake_case.

Validators raise plain ValueError → main.py validation_exception_handler
converts RequestValidationError → 400 VALIDATION_ERROR with error.fields[]
(decision Session 1: collapse granular spec codes — INSUFFICIENT_CHUNKS /
SCRIPT_TOO_LONG / WORD_TIMING_INVALID — into VALIDATION_ERROR for parity with
narrate_script + translate_content).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from src.models.base import FlexibleModel
from src.services.resource_persist import SaveResourceDirective

__all__ = [
    "MIN_CHUNKS",
    "MAX_CHUNKS",
    "MAX_PER_CHUNK_SCRIPT",
    "MAX_TOTAL_SCRIPT",
    "MAX_TOTAL_WORD_TIMINGS",
    "DEFAULT_SEPARATOR",
    "DEFAULT_OUTPUT_FORMAT",
    "WordTimingInput",
    "ChunkInput",
    "CombineAudioChunksRequest",
    "WordTimingOut",
    "CombineData",
    "CombineMeta",
    "CombineResponse",
]

MIN_CHUNKS = 2
MAX_CHUNKS = 50
MAX_PER_CHUNK_SCRIPT = 3_000
MAX_TOTAL_SCRIPT = 50_000
MAX_TOTAL_WORD_TIMINGS = 20_000
DEFAULT_SEPARATOR = "\n"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

OutputFormat = Literal["mp3_44100_128", "mp3_44100_192"]


class WordTimingInput(FlexibleModel):
    """Per-word timing for one input chunk (script-relative offsets)."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=False,
        validate_default=True,
        extra="ignore",
    )

    text: str = Field(min_length=1)
    start_ms: int = Field(ge=0, alias="startMs")
    end_ms: int = Field(ge=0, alias="endMs")
    char_start: int = Field(ge=0, alias="charStart")
    char_end: int = Field(ge=0, alias="charEnd")

    @model_validator(mode="after")
    def _validate_invariants(self) -> "WordTimingInput":
        if self.end_ms < self.start_ms:
            raise ValueError(
                f"endMs ({self.end_ms}) must be >= startMs ({self.start_ms})"
            )
        if self.char_end <= self.char_start:
            raise ValueError(
                f"charEnd ({self.char_end}) must be > charStart ({self.char_start})"
            )
        return self


class ChunkInput(FlexibleModel):
    """Single input chunk: audio URL + script + per-word timings."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=False,
        validate_default=True,
        extra="ignore",
    )

    audio_url: str = Field(min_length=1, alias="audioUrl")
    script: str
    word_timings: list[WordTimingInput] = Field(alias="wordTimings")

    @field_validator("script")
    @classmethod
    def _validate_script(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("script must be non-empty")
        if len(v) > MAX_PER_CHUNK_SCRIPT:
            raise ValueError(
                f"script length {len(v)} exceeds per-chunk cap {MAX_PER_CHUNK_SCRIPT}"
            )
        return v

    @model_validator(mode="after")
    def _validate_word_timings_against_script(self) -> "ChunkInput":
        n = len(self.script)
        last_start = -1
        for i, w in enumerate(self.word_timings):
            if w.char_end > n:
                raise ValueError(
                    f"wordTimings[{i}].charEnd ({w.char_end}) exceeds script length ({n})"
                )
            if w.start_ms < last_start:
                raise ValueError(
                    f"wordTimings[{i}].startMs ({w.start_ms}) is less than previous "
                    f"({last_start}); must be monotonic non-decreasing"
                )
            last_start = w.start_ms
        return self


class CombineAudioChunksRequest(FlexibleModel):
    """Request body for POST /api/text/combine-audio-chunks."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=False,
        validate_default=True,
        extra="ignore",
    )

    chunks: list[ChunkInput] = Field(min_length=MIN_CHUNKS, max_length=MAX_CHUNKS)
    script_separator: str = Field(
        default=DEFAULT_SEPARATOR,
        min_length=1,
        max_length=1,
        alias="scriptSeparator",
    )
    output_format: OutputFormat = Field(
        default=DEFAULT_OUTPUT_FORMAT,
        alias="outputFormat",
    )
    # Opt-in auto-persist (type=textbox_combined_audio). Absent ⇒ no-op.
    save_resource: SaveResourceDirective | None = Field(
        default=None, alias="saveResource"
    )

    @model_validator(mode="after")
    def _validate_aggregate(self) -> "CombineAudioChunksRequest":
        total_script = sum(len(c.script) for c in self.chunks)
        if total_script > MAX_TOTAL_SCRIPT:
            raise ValueError(
                f"sum(chunks.script) {total_script} exceeds total cap {MAX_TOTAL_SCRIPT}"
            )
        total_wt = sum(len(c.word_timings) for c in self.chunks)
        if total_wt > MAX_TOTAL_WORD_TIMINGS:
            raise ValueError(
                f"sum(chunks.wordTimings) {total_wt} exceeds total cap "
                f"{MAX_TOTAL_WORD_TIMINGS}"
            )
        return self


# ─── Response models ─────────────────────────────────────────────────────────


class WordTimingOut(FlexibleModel):
    """Rebased word timing — offsets reference data.joinedScript / combined audio."""

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


class CombineData(FlexibleModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    audio_url: str = Field(alias="audioUrl")
    duration_ms: int = Field(alias="durationMs")
    words: list[WordTimingOut]
    chunk_offsets_ms: list[int] = Field(alias="chunkOffsetsMs")
    joined_script: str = Field(alias="joinedScript")
    # save-generated-resource result (additive; None when no saveResource sent).
    saved: Optional[bool] = None
    snapshot_id: Optional[str] = Field(default=None, alias="snapshotId")
    save_error: Optional[str] = Field(default=None, alias="saveError")


class CombineMeta(FlexibleModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    processing_time_ms: int = Field(alias="processingTimeMs")
    download_ms: int = Field(alias="downloadMs")
    encode_ms: int = Field(alias="encodeMs")
    upload_ms: int = Field(alias="uploadMs")
    fast_path: bool = Field(alias="fastPath")
    reencode_reason: Optional[
        Literal["mp3_gapless_unsafe", "format_mismatch", "codec_mismatch"]
    ] = Field(default=None, alias="reencodeReason")
    path_key: str = Field(alias="pathKey")
    chunk_count: int = Field(alias="chunkCount")
    total_input_size_bytes: int = Field(alias="totalInputSizeBytes")


class CombineResponse(FlexibleModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_default=True,
        extra="ignore",
        serialize_by_alias=True,
    )

    success: bool = True
    data: CombineData
    meta: CombineMeta
