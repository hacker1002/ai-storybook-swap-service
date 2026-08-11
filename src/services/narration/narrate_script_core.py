"""In-process core for POST /api/text/narrate-script.

Identical pipeline to the router (parse → path-key → ElevenLabs TTS-with-timestamps
→ word aggregation → Storage upload) but:
  - Raises `NarrationDomainError` (status/code/message) instead of HTTPException.
  - Returns a plain dataclass (`words` as camelCase dicts ready for JSONB
    persistence) — callers wrap or store as needed.
  - `@traceable` lives here so direct in-process callers (job handler) and
    HTTP callers share the same LangSmith trace tree.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

from langsmith import traceable

from src.config.settings import Settings, settings as settings_singleton
from src.models.requests.narrate_script import (
    NarrateScriptRequest,
    NarrateScriptSettings,
)
from src.services.ai_usage import AiCallContext
from src.services.elevenlabs_client import (
    ElevenAuthFailedError,
    ElevenContentRejectedError,
    ElevenRateLimitedError,
    ElevenTimeoutError,
    ElevenTTSFailedError,
    ElevenUpstreamError,
    ElevenVoiceNotFoundError,
    estimate_cost_usd,
    text_to_speech_with_timestamps,
)
from src.services.narration.errors import NarrationDomainError
from src.services.narration.narration_path import build_path_key
from src.services.narration.narration_word_aggregator import aggregate_single_turn_words
from src.services.narration.script_mention_parser import (
    MultiTurnNotSupportedError,
    ScriptParseError,
    ScriptTooLongError,
    parse_single_turn,
)
from src.services.storage import (
    StorageUploadError,
    build_narration_path,
    upload_bytes,
)

logger = logging.getLogger(__name__)

__all__ = ["NarrateScriptCoreResult", "run_narrate_script"]


@dataclass(frozen=True)
class NarrateScriptCoreResult:
    """Result shape mirrors the wire response data + meta, but `words` is a
    camelCase dict list so callers can persist directly to JSONB without
    re-serializing pydantic models.
    """

    # data fields
    audio_url: str
    duration_ms: int
    voice_id: str
    text: str
    words: list[dict[str, Any]]  # camelCase keys: {text, startMs, endMs, charStart, charEnd}
    raw_alignment: dict[str, Any]

    # meta fields
    processing_time_ms: int
    eleven_call_ms: int
    upload_ms: int
    model_id: str
    path_key: str
    char_count: int
    cost_estimate: Optional[float]
    alignment_fallback: Optional[bool]
    raw_alignment_source: Literal["with_timestamps", "fallback_base"]


def _get_settings() -> Settings:
    return settings_singleton


@traceable(name="text_narrate_script")
async def run_narrate_script(
    request: NarrateScriptRequest,
    *,
    ai_context: AiCallContext | None = None,
) -> NarrateScriptCoreResult:
    app_settings = _get_settings()
    t_start = time.monotonic()
    logger.info(
        "narrate_script_start char_count=%d model=%s output_format=%s",
        len(request.script), request.model_id, request.output_format,
    )

    # 1. Parse single-turn -----------------------------------------------------
    try:
        inp = parse_single_turn(request.script)
    except ScriptTooLongError as exc:
        raise NarrationDomainError(
            status=400, code="SCRIPT_TOO_LONG", message=str(exc)
        ) from exc
    except MultiTurnNotSupportedError as exc:
        raise NarrationDomainError(
            status=400, code="MULTI_TURN_NOT_SUPPORTED", message=str(exc)
        ) from exc
    except ScriptParseError as exc:
        code = (
            "INVALID_VOICE_ID"
            if exc.reason == "voice_id_invalid"
            else "SCRIPT_PARSE_ERROR"
        )
        raise NarrationDomainError(
            status=400,
            code=code,
            message=exc.message,
            details={"reason": exc.reason},
        ) from exc

    # 2. Merge settings defaults ----------------------------------------------
    effective_settings = request.settings or NarrateScriptSettings()
    settings_dict = effective_settings.model_dump(by_alias=False)

    # 3. Path key (hash on text_with_tags so cache differentiates per tag combo)
    path_key = build_path_key(
        voice_id=inp.voice_id,
        text_with_tags=inp.text_with_tags,
        model_id=request.model_id,
        settings_dict=settings_dict,
        output_format=request.output_format,
    )
    narration_path = build_narration_path(path_key)
    char_count = len(inp.text_clean)

    # 4. ElevenLabs TTS-with-timestamps — send text_with_tags for prosody -----
    try:
        raw = await text_to_speech_with_timestamps(
            voice_id=inp.voice_id,
            text=inp.text_with_tags,
            model_id=request.model_id,
            stability=effective_settings.stability,
            similarity_boost=effective_settings.similarity_boost,
            style=effective_settings.style,
            speed=effective_settings.speed,
            output_format=request.output_format,
            settings=app_settings,
            seed=effective_settings.seed,
            ai_context=ai_context,
        )
    except ElevenVoiceNotFoundError as exc:
        raise NarrationDomainError(
            status=404, code="ELEVEN_VOICE_NOT_FOUND", message=str(exc)
        ) from exc
    except ElevenContentRejectedError as exc:
        raise NarrationDomainError(
            status=422, code="ELEVEN_CONTENT_REJECTED", message=str(exc)
        ) from exc
    except ElevenRateLimitedError as exc:
        raise NarrationDomainError(
            status=429, code="ELEVEN_RATE_LIMITED", message=str(exc)
        ) from exc
    except ElevenTimeoutError as exc:
        raise NarrationDomainError(
            status=504, code="TIMEOUT", message=str(exc)
        ) from exc
    except ElevenAuthFailedError as exc:
        logger.error("narrate_script_auth_failed — check ELEVENLABS_API_KEY")
        raise NarrationDomainError(
            status=500,
            code="ELEVEN_AUTH_FAILED",
            message="Upstream auth misconfigured",
        ) from exc
    except (ElevenUpstreamError, ElevenTTSFailedError) as exc:
        raise NarrationDomainError(
            status=502, code="ELEVEN_UPSTREAM_ERROR", message=str(exc)
        ) from exc

    # 5. Aggregate words on text_clean ----------------------------------------
    word_spans, alignment_fallback = aggregate_single_turn_words(
        text_clean=inp.text_clean,
        alignment_chars=raw.alignment_chars,
        alignment_start_s=raw.alignment_start_s,
        alignment_end_s=raw.alignment_end_s,
    )
    if raw.has_alignment and raw.alignment_end_s:
        duration_ms = int(raw.alignment_end_s[-1] * 1000)
    elif word_spans:
        duration_ms = word_spans[-1].end_ms
    else:
        duration_ms = 0

    # 6. Upload to Supabase Storage with upsert dedup -------------------------
    t_upload = time.monotonic()
    try:
        public_url = await upload_bytes(
            path=narration_path,
            body=raw.audio_bytes,
            content_type=raw.mime_type,
            upsert=True,
        )
    except StorageUploadError as exc:
        logger.error(
            "narrate_script_storage_failed path_key=%s reason=%s",
            path_key[:16], exc.reason,
        )
        raise NarrationDomainError(
            status=500,
            code="STORAGE_UPLOAD_ERROR",
            message="Failed to persist audio",
        ) from exc
    upload_ms = int((time.monotonic() - t_upload) * 1000)

    # 7. Build result ----------------------------------------------------------
    processing_ms = int((time.monotonic() - t_start) * 1000)
    cost = estimate_cost_usd(char_count, request.model_id)
    raw_alignment_source: Literal["with_timestamps", "fallback_base"] = (
        "with_timestamps" if raw.raw_alignment_from_timestamps else "fallback_base"
    )
    logger.info(
        "narrate_script_done path_key=%s duration_ms=%d cost=$%.4f "
        "processing_ms=%d eleven_ms=%d upload_ms=%d align_fallback=%s words=%d",
        path_key[:16], duration_ms, cost, processing_ms, raw.eleven_ms,
        upload_ms, alignment_fallback, len(word_spans),
    )

    return NarrateScriptCoreResult(
        audio_url=public_url,
        duration_ms=duration_ms,
        voice_id=inp.voice_id,
        text=inp.text_clean,
        words=[
            {
                "text": w.text,
                "startMs": w.start_ms,
                "endMs": w.end_ms,
                "charStart": w.char_start,
                "charEnd": w.char_end,
            }
            for w in word_spans
        ],
        raw_alignment=raw.raw_alignment,
        processing_time_ms=processing_ms,
        eleven_call_ms=raw.eleven_ms,
        upload_ms=upload_ms,
        model_id=request.model_id,
        path_key=path_key,
        char_count=char_count,
        cost_estimate=cost,
        alignment_fallback=alignment_fallback or None,
        raw_alignment_source=raw_alignment_source,
    )
