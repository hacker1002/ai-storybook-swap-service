"""ElevenLabs client — Text-to-Voice Design v3 proxy.

Shared by `/api/voice/*` endpoints (generate-from-prompt, get-from-eleven-id,
save-preview). Typed exceptions are mapped to HTTP responses by the router layer.

Mapping notes:
    loudness_ui [0,1]      -> eleven loudness [-1,1]   (x*2 - 1)
    guidance_ui [0,1]      -> eleven guidance_scale    (x*100)

Language whitelist aligned with FE SUPPORTED_LANGUAGES
(ai-storybook-editor/src/constants/config-constants.ts:52). Extend requires both
FE+BE update.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from src.config.settings import Settings
from src.models.requests.voice_generate import (
    GenerateFromPromptRequest,
    PreviewCandidate,
)
from src.services.ai_usage import (
    AiCallContext,
    AiLogEntry,
    compute_cost,
    log_ai_request,
    new_request_id,
    sanitize_request,
)
from src.services.ai_usage.pricing import ELEVENLABS_CHAR_PRICING
from src.services.elevenlabs_rate_limit import acquire_eleven_slot

__all__ = [
    "design_voice_previews",
    "get_voice",
    "lookup_shared_voice",
    "save_preview",
    "delete_voice",
    "DesignResult",
    "SavePreviewResult",
    "ElevenDesignError",
    "UnsupportedLanguageError",
    "ElevenDesignFailedError",
    "ElevenRateLimitedError",
    "ElevenUpstreamError",
    "ElevenTimeoutError",
    "ElevenVoiceNotFoundError",
    "ElevenSharedVoiceNotFoundError",
    "ElevenAuthFailedError",
    "ElevenPreviewExpiredError",
    "ElevenVoiceLimitError",
    "ElevenSaveFailedError",
    "ElevenContentRejectedError",
    "ElevenTTSFailedError",
    "SingleTurnRawResult",
    "text_to_speech_with_timestamps",
    "estimate_cost_usd",
    "ELEVEN_TTS_MODEL_PRICING_USD_PER_1K",
    "ELEVEN_TTV_V3_MODEL_ID",
    "ELEVEN_TTV_V3_SUPPORTED_LANGUAGES",
    "ELEVEN_GET_VOICE_TIMEOUT_S",
    "ELEVEN_SHARED_VOICES_TIMEOUT_S",
    "ELEVEN_SAVE_PREVIEW_TIMEOUT_S",
    "ELEVEN_DELETE_VOICE_TIMEOUT_S",
    # SFX additions
    "SoundEffectRawResult",
    "ElevenSfxDurationError",
    "ElevenSfxFailedError",
    "generate_sound_effect",
    "ELEVEN_SFX_PATH",
    "ELEVEN_SFX_TIMEOUT_S",
    # Music additions
    "MusicComposeRawResult",
    "ElevenMusicPaymentRequiredError",
    "ElevenMusicContentRejectedError",
    "ElevenMusicDurationOutOfRangeError",
    "ElevenMusicGenerateFailedError",
    "ElevenMusicRateLimitedError",
    "ElevenMusicAuthFailedError",
    "ElevenMusicUpstreamError",
    "ElevenMusicTimeoutError",
    "ElevenMusicInternalError",
    "compose_music",
    "ELEVEN_MUSIC_PATH",
    "ELEVEN_MUSIC_TIMEOUT_S",
    # IVC (clone-from-human) additions
    "ivc_create",
    "tts_preview",
    "IvcCreateResult",
    "TtsPreviewResult",
    "ElevenIvcFailedError",
    "ElevenTtsPreviewFailedError",
    "ELEVEN_IVC_PATH",
    "ELEVEN_IVC_TIMEOUT_S",
    "ELEVEN_TTS_PREVIEW_TIMEOUT_S",
]


logger = logging.getLogger(__name__)


ELEVEN_API_BASE = "https://api.elevenlabs.io"
ELEVEN_DESIGN_PATH = "/v1/text-to-voice/design"
ELEVEN_GET_VOICE_PATH = "/v1/voices"
ELEVEN_SHARED_VOICES_PATH = "/v1/shared-voices"
ELEVEN_TTV_V3_MODEL_ID = "eleven_ttv_v3"
ELEVEN_DESIGN_TIMEOUT_S = 45.0
ELEVEN_GET_VOICE_TIMEOUT_S = 10.0
ELEVEN_SHARED_VOICES_TIMEOUT_S = 10.0
ELEVEN_SAVE_PREVIEW_TIMEOUT_S = 15.0
ELEVEN_DELETE_VOICE_TIMEOUT_S = 5.0
ELEVEN_SAVE_PREVIEW_PATH = "/v1/text-to-voice"  # body carries generated_voice_id
ELEVEN_TTS_TIMESTAMPS_PATH_TMPL = "/v1/text-to-speech/{voice_id}/with-timestamps"
ELEVEN_TTS_BASE_PATH_TMPL = "/v1/text-to-speech/{voice_id}"
ELEVEN_TTS_TIMEOUT_S = 60.0
ELEVEN_SFX_PATH = "/v1/sound-generation"
ELEVEN_SFX_TIMEOUT_S = 60.0
ELEVEN_MUSIC_PATH = "/v1/music/compose"
# Music gen p95 ~30-90s per spec; 180s gives headroom for the long tail.
ELEVEN_MUSIC_TIMEOUT_S = 180.0

# USD per 1,000 characters. The pricing table MOVED to the single AI-usage source
# `ai_usage.pricing.ELEVENLABS_CHAR_PRICING` (ADR-050) — this name is a backward-compat
# re-export (kept in `__all__`); `estimate_cost_usd` now delegates to `compute_cost`.
ELEVEN_TTS_MODEL_PRICING_USD_PER_1K = ELEVENLABS_CHAR_PRICING

# Align with FE SUPPORTED_LANGUAGES (config-constants.ts:52). Extending requires
# both FE (language picker) and BE (this set + PREVIEW_TEXT_BY_LANGUAGE) updates.
ELEVEN_TTV_V3_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"en_US", "vi_VN", "ja_JP", "ko_KR", "zh_CN"}
)

GENDER_LABEL: dict[int, str] = {0: "female", 1: "male"}
AGE_LABEL: dict[int, str] = {0: "young", 1: "middle_aged", 2: "old"}

# Min 120 chars so ElevenLabs accepts the sample. All whitelisted languages have
# a native template so design-time audition matches runtime narration phonetics.
# Defense-in-depth: _pick_preview_text still falls back to en_US + WARN log if a
# future language enters the whitelist before its template ships.
PREVIEW_TEXT_BY_LANGUAGE: dict[str, str] = {
    "en_US": (
        "Once upon a time, in a land far away, a small dragon discovered a hidden secret that would change everything."
    ),
    "vi_VN": (
        "Ngày xửa ngày xưa, ở một vùng đất xa xôi, một chú rồng nhỏ đã phát hiện ra một bí mật ẩn giấu có thể thay đổi tất cả."
    ),
    "ja_JP": (
        "むかしむかし、遠い国に小さなドラゴンがいました。ある日、すべてを変える秘密を発見しました。"
    ),
    "ko_KR": (
        "옛날 옛적에, 먼 나라에 작은 용이 모든 것을 바꿀 숨겨진 비밀을 발견했습니다."
    ),
    "zh_CN": (
        "很久很久以前，在一个遥远的地方，一条小龙发现了一个能改变一切的隐藏秘密。"
    ),
}

# Heuristic cue tokens for the prepend-skip check.
_GENDER_CUES: dict[int, tuple[str, ...]] = {
    0: ("female", "woman", "girl", "lady"),
    1: ("male", "man", "boy", "gentleman"),
}
_AGE_CUES: dict[int, tuple[str, ...]] = {
    0: ("young", "youthful", "child", "kid"),
    1: ("middle-aged", "middle aged", "adult", "mature"),
    2: ("old", "elderly", "senior", "aged"),
}


# ──────────────────────────────────────────────────────────────────────────────
# Typed exceptions (router layer maps to HTTP responses)
# ──────────────────────────────────────────────────────────────────────────────


class ElevenDesignError(Exception):
    """Base class for all ElevenLabs design errors."""


class UnsupportedLanguageError(ElevenDesignError):
    def __init__(self, language: str) -> None:
        super().__init__(f"Language '{language}' not supported by eleven_ttv_v3")
        self.language = language


class ElevenDesignFailedError(ElevenDesignError):
    """ElevenLabs returned 4xx (not 429) — content safety / invalid param / empty previews."""

    def __init__(self, eleven_status: int, body: str) -> None:
        super().__init__(f"ElevenLabs design failed ({eleven_status}): {body[:200]}")
        self.eleven_status = eleven_status
        self.body = body


class ElevenRateLimitedError(ElevenDesignError):
    """ElevenLabs 429 — quota or concurrency."""


class ElevenUpstreamError(ElevenDesignError):
    """ElevenLabs 5xx."""

    def __init__(self, eleven_status: int) -> None:
        super().__init__(f"ElevenLabs upstream error ({eleven_status})")
        self.eleven_status = eleven_status


class ElevenTimeoutError(ElevenDesignError):
    """Request exceeded ELEVEN_DESIGN_TIMEOUT_S."""


class ElevenVoiceNotFoundError(ElevenDesignError):
    """ElevenLabs returned 404 for GET /v1/voices/{voice_id}."""

    def __init__(self, voice_id: str) -> None:
        super().__init__(f"Voice '{voice_id}' not found on ElevenLabs")
        self.voice_id = voice_id


class ElevenSharedVoiceNotFoundError(ElevenDesignError):
    """Shared voice not matched in public library search results.

    Distinct from ElevenVoiceNotFoundError so handler can orchestrate the
    personal→shared fallback without conflating the two miss paths.
    """

    def __init__(self, voice_id: str) -> None:
        super().__init__(f"Shared voice '{voice_id}' not found in public library")
        self.voice_id = voice_id


class ElevenAuthFailedError(ElevenDesignError):
    """ElevenLabs returned 401 — service-side API key misconfig (not user-facing)."""

    def __init__(self) -> None:
        super().__init__("ElevenLabs API key auth failed")


class ElevenPreviewExpiredError(ElevenDesignError):
    """ElevenLabs 404 on save — preview ID unknown/expired (>72h or bad ID)."""

    def __init__(self, generated_voice_id: str, body: str = "") -> None:
        super().__init__(
            f"Generated voice id '{generated_voice_id}' expired or not found"
        )
        self.generated_voice_id = generated_voice_id
        self.body = body


class ElevenVoiceLimitError(ElevenDesignError):
    """ElevenLabs 409 on save — voice library quota exceeded for the plan."""

    def __init__(self, body: str = "") -> None:
        super().__init__("ElevenLabs voice library limit reached")
        self.body = body


class ElevenSaveFailedError(ElevenDesignError):
    """ElevenLabs returned 4xx (other than 401/404/409/429) on save —
    validation / invalid labels / content safety / etc."""

    def __init__(self, eleven_status: int, body: str) -> None:
        super().__init__(f"ElevenLabs save failed ({eleven_status}): {body[:200]}")
        self.eleven_status = eleven_status
        self.body = body


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DesignResult:
    previews: list[PreviewCandidate]
    preview_text: str
    seed_used: int
    eleven_design_ms: int


def _resolve_seed(client_seed: int | None) -> int:
    """Client-supplied wins; otherwise random within ElevenLabs int32 seed range."""
    if client_seed is not None:
        return client_seed
    return random.randint(0, 2**31 - 1)


def _build_voice_description(req: GenerateFromPromptRequest) -> str:
    """Prepend `{gender}, {age}, {accent} accent.` unless description already hints them.

    Over-prepending is safer than missing — duplicate cues don't break voice quality,
    but missing cues can yield wrong gender/age output.

    Sentinel `accent == "any"` (or "neutral" legacy) skips the accent hint entirely —
    matches ElevenLabs URL convention (omit accent param when no preference).
    """
    gender_word = GENDER_LABEL[req.gender]
    age_word = AGE_LABEL[req.age].replace("_", "-")
    accent_word = (req.accent or "any").strip().lower()
    desc_lower = req.description.lower()

    has_gender_cue = any(c in desc_lower for c in _GENDER_CUES[req.gender])
    has_age_cue = any(c in desc_lower for c in _AGE_CUES[req.age])

    if has_gender_cue and has_age_cue:
        return req.description
    if accent_word in ("any", "neutral"):
        return f"{gender_word}, {age_word}. {req.description}"
    return f"{gender_word}, {age_word}, {accent_word} accent. {req.description}"


def _pick_preview_text(language: str) -> str:
    text = PREVIEW_TEXT_BY_LANGUAGE.get(language)
    if text is None:
        logger.warning(
            "voice_preview_text_fallback language=%s fallback=en_US", language
        )
        return PREVIEW_TEXT_BY_LANGUAGE["en_US"]
    return text


def _classify_response_error(response: httpx.Response) -> ElevenDesignError:
    status = response.status_code
    try:
        body_text = response.text
    except Exception:  # noqa: BLE001 — defensive
        body_text = ""

    if status == 429:
        return ElevenRateLimitedError("ElevenLabs rate limited")
    if 500 <= status < 600:
        return ElevenUpstreamError(eleven_status=status)
    # 4xx other than 429 → treat as design failed (prompt safety / validation).
    return ElevenDesignFailedError(eleven_status=status, body=body_text)


def _transform_previews(body: dict[str, Any]) -> list[PreviewCandidate]:
    raw_previews = body.get("previews") or []
    out: list[PreviewCandidate] = []
    for p in raw_previews:
        try:
            out.append(
                PreviewCandidate(
                    generated_voice_id=p["generated_voice_id"],
                    audio_base64=p["audio_base_64"],
                    media_type=p.get("media_type", "audio/mpeg"),
                    duration_secs=float(p["duration_secs"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ElevenDesignFailedError(
                eleven_status=200,
                body=f"preview schema mismatch: {exc}",
            ) from exc
    return out


# ──────────────────────────────────────────────────────────────────────────────
# LangSmith input filter — MUST strip `settings` (contains all service secrets:
# elevenlabs_api_key, supabase_service_role_key, replicate_api_token, etc.).
# Applied to every @traceable in this module that takes a Settings arg.
# ──────────────────────────────────────────────────────────────────────────────


def _strip_settings_from_trace(inputs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in inputs.items() if k != "settings"}


def _attach_run_metadata(metadata: dict[str, Any]) -> None:
    """Best-effort attach metadata to current LangSmith run. No-op if no active run."""
    try:
        run = get_current_run_tree()
        if run is not None:
            run.add_metadata(metadata)
    except Exception:  # noqa: BLE001 — tracing must never break business logic
        logger.debug("langsmith_metadata_attach_failed", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────────────────


@traceable(name="elevenlabs_design", process_inputs=_strip_settings_from_trace)
async def design_voice_previews(
    req: GenerateFromPromptRequest,
    settings: Settings,
    ai_context: AiCallContext | None = None,
) -> DesignResult:
    """Call ElevenLabs Design v3 and transform the response.

    Raises ElevenDesignError subclasses on failure; router maps to HTTP status.

    Logs one `ai_service_logs` row per design HTTP call (ADR-050 / Validation S1 —
    previously UNLOGGED). The N preview candidates are the raw output → persisted
    content-addressed to `ai-logs/outputs/` via `output_blobs` (provenance only —
    previews are not an Illustration Entry sink, so NO `data.media_url` is surfaced).
    """
    if req.language not in ELEVEN_TTV_V3_SUPPORTED_LANGUAGES:
        raise UnsupportedLanguageError(req.language)

    voice_description = _build_voice_description(req)
    preview_text = _pick_preview_text(req.language)
    seed_used = _resolve_seed(req.seed)

    payload = {
        "voice_description": voice_description,
        "text": preview_text,
        "model_id": ELEVEN_TTV_V3_MODEL_ID,
        "loudness": req.loudness * 2 - 1,
        "guidance_scale": req.guidance * 100,
        "seed": seed_used,
        "auto_generate_text": False,
    }

    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
    }

    # Expose the exact ElevenLabs request payload to LangSmith for debugging
    # (secrets already stripped via process_inputs; headers never traced).
    _attach_run_metadata({"eleven_request_payload": payload})

    logger.info(
        "elevenlabs_design_start language=%s description_length=%d gender=%d age=%d seed=%d voice_description=%s",
        req.language, len(req.description), req.gender, req.age, seed_used,
        voice_description[:200],
    )

    await acquire_eleven_slot("design_voice")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_DESIGN_TIMEOUT_S) as client:
            response = await client.post(
                f"{ELEVEN_API_BASE}{ELEVEN_DESIGN_PATH}",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        logger.warning("elevenlabs_design_timeout elapsed_s=%.1f", time.monotonic() - t0)
        raise ElevenTimeoutError("Design request timed out") from exc

    eleven_design_ms = int((time.monotonic() - t0) * 1000)

    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_design_http_error status=%d elapsed_ms=%d body_excerpt=%s",
            response.status_code, eleven_design_ms, response.text[:200],
        )
        _log_elevenlabs_call(
            ctx=ai_context, operation="voice.design_previews", model_id=ELEVEN_TTV_V3_MODEL_ID,
            char_count=len(preview_text), request_payload=payload,
            provider_request_id=_eleven_request_id(response),
            status="error", error=f"HTTP {response.status_code}: {response.text[:200]}",
        )
        raise _classify_response_error(response)

    try:
        body = response.json()
    except ValueError as exc:
        raise ElevenDesignFailedError(
            eleven_status=response.status_code,
            body=f"non-JSON response: {response.text[:200]}",
        ) from exc

    previews = _transform_previews(body)
    if not previews:
        raise ElevenDesignFailedError(
            eleven_status=response.status_code, body="empty previews"
        )

    logger.info(
        "elevenlabs_design_done preview_count=%d eleven_design_ms=%d seed=%d",
        len(previews), eleven_design_ms, seed_used,
    )

    # Each preview candidate = raw audio output → persist content-addressed
    # `ai-logs/outputs/` (decoded here; the insert thread does the PUT). A bad
    # candidate never breaks the log/request.
    out_blobs: list[tuple] = []
    for p in previews:
        try:
            out_blobs.append((base64.b64decode(p.audio_base64), "audio/mpeg"))
        except Exception:  # noqa: BLE001 — malformed preview → skip, never fail
            continue
    _log_elevenlabs_call(
        ctx=ai_context, operation="voice.design_previews", model_id=ELEVEN_TTV_V3_MODEL_ID,
        char_count=len(preview_text), request_payload=payload,
        provider_request_id=_eleven_request_id(response),
        output_blobs=tuple(out_blobs),
    )

    return DesignResult(
        previews=previews,
        preview_text=preview_text,
        seed_used=seed_used,
        eleven_design_ms=eleven_design_ms,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /v1/voices/{voice_id} — metadata proxy for Import Voice modal
# ──────────────────────────────────────────────────────────────────────────────


@traceable(name="elevenlabs_get_voice", process_inputs=_strip_settings_from_trace)
async def get_voice(voice_id: str, settings: Settings) -> dict[str, Any]:
    """Fetch ElevenLabs voice metadata. Router layer maps exceptions to HTTP."""
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Accept": "application/json",
    }
    url = f"{ELEVEN_API_BASE}{ELEVEN_GET_VOICE_PATH}/{voice_id}"

    logger.info(
        "elevenlabs_get_voice_start voice_id=%s method=GET url=%s timeout_s=%.1f",
        voice_id, url, ELEVEN_GET_VOICE_TIMEOUT_S,
    )
    # Expose request shape to LangSmith for debugging (API key never traced —
    # stripped via process_inputs, headers not passed).
    _attach_run_metadata({
        "eleven_request": {
            "method": "GET",
            "url": url,
            "voice_id": voice_id,
            "timeout_s": ELEVEN_GET_VOICE_TIMEOUT_S,
        }
    })

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_GET_VOICE_TIMEOUT_S) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_get_voice_timeout voice_id=%s url=%s elapsed_s=%.1f",
            voice_id, url, time.monotonic() - t0,
        )
        raise ElevenTimeoutError("Get voice request timed out") from exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    status = response.status_code

    if status == 404:
        logger.info(
            "elevenlabs_get_voice_not_found voice_id=%s elapsed_ms=%d",
            voice_id, elapsed_ms,
        )
        raise ElevenVoiceNotFoundError(voice_id)
    # ElevenLabs emits HTTP 400 with `detail.code == "voice_not_found"` for
    # well-formed-but-missing voice IDs (not 404 as spec assumed). Parse.
    if status == 400:
        body_text = response.text
        try:
            body_json = response.json()
        except ValueError:
            body_json = None
        detail_code = None
        if isinstance(body_json, dict):
            detail = body_json.get("detail")
            if isinstance(detail, dict):
                detail_code = detail.get("code")
        if detail_code == "voice_not_found":
            logger.info(
                "elevenlabs_get_voice_not_found voice_id=%s elapsed_ms=%d (via 400)",
                voice_id, elapsed_ms,
            )
            raise ElevenVoiceNotFoundError(voice_id)
        logger.warning(
            "elevenlabs_get_voice_bad_request voice_id=%s elapsed_ms=%d body_excerpt=%s",
            voice_id, elapsed_ms, body_text[:200],
        )
        raise ElevenUpstreamError(eleven_status=status)
    if status == 401:
        logger.error(
            "elevenlabs_get_voice_auth_failed voice_id=%s elapsed_ms=%d — CHECK ELEVENLABS_API_KEY",
            voice_id, elapsed_ms,
        )
        raise ElevenAuthFailedError()
    if status == 429:
        logger.warning(
            "elevenlabs_get_voice_rate_limited voice_id=%s elapsed_ms=%d",
            voice_id, elapsed_ms,
        )
        raise ElevenRateLimitedError("ElevenLabs rate limited")
    if status >= 500:
        logger.warning(
            "elevenlabs_get_voice_upstream_error voice_id=%s status=%d elapsed_ms=%d",
            voice_id, status, elapsed_ms,
        )
        raise ElevenUpstreamError(eleven_status=status)
    if status >= 400:
        logger.warning(
            "elevenlabs_get_voice_http_error voice_id=%s status=%d elapsed_ms=%d body_excerpt=%s",
            voice_id, status, elapsed_ms, response.text[:200],
        )
        # Treat other 4xx (e.g., 400 malformed id shape) as upstream for router mapping.
        raise ElevenUpstreamError(eleven_status=status)

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "elevenlabs_get_voice_non_json voice_id=%s status=%d body_excerpt=%s",
            voice_id, status, response.text[:200],
        )
        raise ElevenUpstreamError(eleven_status=status) from exc

    logger.info(
        "elevenlabs_get_voice_done voice_id=%s status=%d elapsed_ms=%d body_bytes=%d",
        voice_id, status, elapsed_ms, len(response.content),
    )
    logger.debug(
        "elevenlabs_get_voice_body voice_id=%s body_excerpt=%s",
        voice_id, response.text[:200].replace("\n", " "),
    )
    return body


# ──────────────────────────────────────────────────────────────────────────────
# POST /v1/text-to-voice/{generated_voice_id} — persist preview → permanent voice
# Shared by save-preview handler. Sibling of design_voice_previews.
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SavePreviewResult:
    voice_id: str
    name: str
    eleven_save_ms: int
    raw: dict[str, Any]


def _classify_save_error(
    generated_voice_id: str, resp: httpx.Response
) -> ElevenDesignError:
    status = resp.status_code
    try:
        body_text = resp.text
    except Exception:  # noqa: BLE001 — defensive
        body_text = ""
    if status == 404:
        return ElevenPreviewExpiredError(generated_voice_id, body=body_text[:200])
    if status == 409:
        return ElevenVoiceLimitError(body=body_text[:200])
    if status in (401, 403):
        return ElevenAuthFailedError()
    if status == 429:
        return ElevenRateLimitedError("ElevenLabs rate limited")
    if 500 <= status < 600:
        return ElevenUpstreamError(eleven_status=status)
    # 400 variants — ElevenLabs returns `{"detail":{"status":"...","message":"..."}}`
    # for invalid/consumed preview IDs ("already been created", "not found",
    # "expired"). Map to preview-expired so FE prompts regenerate.
    if status == 400:
        msg_lower = ""
        try:
            payload = resp.json()
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                msg_lower = (detail.get("message") or "").lower()
            elif isinstance(detail, str):
                msg_lower = detail.lower()
        except ValueError:
            pass
        if any(
            kw in msg_lower
            for kw in ("already been created", "not found", "expired", "invalid generated_voice_id")
        ):
            return ElevenPreviewExpiredError(generated_voice_id, body=body_text[:200])
    # 4xx catch-all (422 invalid labels, other 400, etc.)
    return ElevenSaveFailedError(eleven_status=status, body=body_text)


@traceable(name="elevenlabs_save_preview", process_inputs=_strip_settings_from_trace)
async def save_preview(
    generated_voice_id: str,
    name: str,
    description: str,
    labels: dict[str, str],
    rejected_ids: list[str] | None,
    settings: Settings,
) -> SavePreviewResult:
    """Persist a temp preview into the permanent ElevenLabs voice library.

    Raises `ElevenDesignError` subclasses on failure; router maps to HTTP status.
    """
    url = f"{ELEVEN_API_BASE}{ELEVEN_SAVE_PREVIEW_PATH}"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
    }
    # Per ElevenLabs API: generated_voice_id travels in body (not path). If the
    # preview has been consumed or expired, upstream returns 400
    # `{"detail":{"status":"...","message":"..."}}` — classify as "expired"
    # so FE prompts regenerate.
    body = {
        "generated_voice_id": generated_voice_id,
        "voice_name": name,
        "voice_description": description,
        "labels": labels,
        "played_not_selected_voice_ids": rejected_ids or [],
    }

    _attach_run_metadata({"eleven_request_payload": body})

    logger.info(
        "elevenlabs_save_preview_start generated_voice_id=%s name_len=%d desc_len=%d rejected_count=%d labels=%s",
        generated_voice_id, len(name), len(description), len(rejected_ids or []),
        labels,
    )

    await acquire_eleven_slot("save_preview")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_SAVE_PREVIEW_TIMEOUT_S) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_save_preview_timeout generated_voice_id=%s elapsed_s=%.1f",
            generated_voice_id, time.monotonic() - t0,
        )
        raise ElevenTimeoutError("Save preview request timed out") from exc

    eleven_save_ms = int((time.monotonic() - t0) * 1000)

    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_save_preview_http_error generated_voice_id=%s status=%d elapsed_ms=%d body_excerpt=%s",
            generated_voice_id, response.status_code, eleven_save_ms,
            response.text[:200],
        )
        raise _classify_save_error(generated_voice_id, response)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ElevenSaveFailedError(
            eleven_status=response.status_code,
            body=f"non-JSON response: {response.text[:200]}",
        ) from exc

    voice_id = payload.get("voice_id")
    if not isinstance(voice_id, str) or not voice_id:
        raise ElevenSaveFailedError(
            eleven_status=response.status_code,
            body=f"missing voice_id in response: {response.text[:200]}",
        )

    logger.info(
        "elevenlabs_save_preview_done generated_voice_id=%s voice_id=%s eleven_save_ms=%d",
        generated_voice_id, voice_id, eleven_save_ms,
    )
    return SavePreviewResult(
        voice_id=voice_id,
        name=str(payload.get("name") or name),
        eleven_save_ms=eleven_save_ms,
        raw=payload,
    )


# ──────────────────────────────────────────────────────────────────────────────
# DELETE /v1/voices/{voice_id} — best-effort compensation rollback
# Returns True on success / 404 (idempotent). False on other failures (logged).
# Never raises — compensation must not mask the original error.
# ──────────────────────────────────────────────────────────────────────────────


@traceable(name="elevenlabs_delete_voice", process_inputs=_strip_settings_from_trace)
async def delete_voice(voice_id: str, settings: Settings) -> bool:
    """Best-effort DELETE of ElevenLabs voice. Used by compensation path only.

    Returns:
        True if voice was deleted or already absent (idempotent success).
        False on any error — logged at WARN for ops alerting.
    """
    url = f"{ELEVEN_API_BASE}{ELEVEN_GET_VOICE_PATH}/{voice_id}"
    headers = {"xi-api-key": settings.elevenlabs_api_key}

    await acquire_eleven_slot("delete_voice")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_DELETE_VOICE_TIMEOUT_S) as client:
            response = await client.delete(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — best-effort: swallow + log
        logger.warning(
            "elevenlabs_delete_voice_exception voice_id=%s elapsed_s=%.1f error=%s",
            voice_id, time.monotonic() - t0, exc.__class__.__name__,
        )
        return False

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    status = response.status_code

    if status in (200, 204, 404):
        logger.info(
            "elevenlabs_delete_voice_ok voice_id=%s status=%d elapsed_ms=%d",
            voice_id, status, elapsed_ms,
        )
        return True

    logger.warning(
        "elevenlabs_delete_voice_failed voice_id=%s status=%d elapsed_ms=%d body_excerpt=%s",
        voice_id, status, elapsed_ms, response.text[:200],
    )
    return False


# ──────────────────────────────────────────────────────────────────────────────
# GET /v1/shared-voices?search={id} — public library fallback for get-from-eleven-id
# Fail-fast: only scan page 1 of results; exact voice_id match required.
# Returns raw shared-voice item (shape differs from /v1/voices/{id} — see
# docs/api/voice/02-get-from-eleven-id.md for mapping).
# ──────────────────────────────────────────────────────────────────────────────


@traceable(name="elevenlabs_lookup_shared_voice", process_inputs=_strip_settings_from_trace)
async def lookup_shared_voice(voice_id: str, settings: Settings) -> dict[str, Any]:
    """Fallback lookup when personal /v1/voices/{id} misses.

    Searches the public Voice Library. `?search=` is fulltext — we MUST filter
    exact `voice_id` client-side; don't trust ordering. No pagination: if the
    first page doesn't contain an exact match, treat as not-found (fail-fast).

    Raises:
        ElevenSharedVoiceNotFoundError: page 1 contains no matching voice_id
        ElevenAuthFailedError: 401 (ops: check ELEVENLABS_API_KEY)
        ElevenRateLimitedError: 429
        ElevenUpstreamError: 4xx (non-auth/rate) or 5xx or non-JSON body
        ElevenTimeoutError: request exceeded ELEVEN_SHARED_VOICES_TIMEOUT_S
    """
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Accept": "application/json",
    }
    url = f"{ELEVEN_API_BASE}{ELEVEN_SHARED_VOICES_PATH}"
    params = {"search": voice_id}

    logger.info(
        "elevenlabs_shared_lookup_start voice_id=%s method=GET url=%s params=%s timeout_s=%.1f",
        voice_id, url, params, ELEVEN_SHARED_VOICES_TIMEOUT_S,
    )
    _attach_run_metadata({
        "eleven_request": {
            "method": "GET",
            "url": url,
            "params": params,
            "voice_id": voice_id,
            "timeout_s": ELEVEN_SHARED_VOICES_TIMEOUT_S,
        }
    })

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_SHARED_VOICES_TIMEOUT_S) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_shared_lookup_timeout voice_id=%s url=%s elapsed_s=%.1f",
            voice_id, url, time.monotonic() - t0,
        )
        raise ElevenTimeoutError("Shared voices lookup timed out") from exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    status = response.status_code

    if status == 401:
        logger.error(
            "elevenlabs_shared_lookup_auth_failed voice_id=%s elapsed_ms=%d — CHECK ELEVENLABS_API_KEY",
            voice_id, elapsed_ms,
        )
        raise ElevenAuthFailedError()
    if status == 429:
        logger.warning(
            "elevenlabs_shared_lookup_rate_limited voice_id=%s elapsed_ms=%d",
            voice_id, elapsed_ms,
        )
        raise ElevenRateLimitedError("ElevenLabs rate limited")
    if status >= 500:
        logger.warning(
            "elevenlabs_shared_lookup_upstream_error voice_id=%s status=%d elapsed_ms=%d",
            voice_id, status, elapsed_ms,
        )
        raise ElevenUpstreamError(eleven_status=status)
    if status >= 400:
        logger.warning(
            "elevenlabs_shared_lookup_http_error voice_id=%s status=%d elapsed_ms=%d body_excerpt=%s",
            voice_id, status, elapsed_ms, response.text[:200],
        )
        raise ElevenUpstreamError(eleven_status=status)

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "elevenlabs_shared_lookup_non_json voice_id=%s status=%d body_excerpt=%s",
            voice_id, status, response.text[:200],
        )
        raise ElevenUpstreamError(eleven_status=status) from exc

    voices = body.get("voices") if isinstance(body, dict) else None
    if not isinstance(voices, list):
        logger.warning(
            "elevenlabs_shared_lookup_schema_drift voice_id=%s body_keys=%s",
            voice_id, list(body.keys()) if isinstance(body, dict) else None,
        )
        raise ElevenUpstreamError(eleven_status=status)

    total = body.get("total_count") if isinstance(body, dict) else None
    has_more = body.get("has_more") if isinstance(body, dict) else None

    for item in voices:
        if isinstance(item, dict) and item.get("voice_id") == voice_id:
            owner_prefix = (item.get("public_owner_id") or "")[:8]
            logger.info(
                "elevenlabs_shared_lookup_found voice_id=%s status=%d elapsed_ms=%d "
                "total=%s has_more=%s page_size=%d owner_prefix=%s",
                voice_id, status, elapsed_ms, total, has_more, len(voices),
                owner_prefix,
            )
            return item

    logger.info(
        "elevenlabs_shared_lookup_not_found voice_id=%s status=%d elapsed_ms=%d "
        "total=%s has_more=%s page_size=%d",
        voice_id, status, elapsed_ms, total, has_more, len(voices),
    )
    raise ElevenSharedVoiceNotFoundError(voice_id)



# ──────────────────────────────────────────────────────────────────────────────
# POST /v1/text-to-speech/{voice_id}/with-timestamps — single-turn TTS w/ alignment
# Consumed by `/api/text/narrate-script` (single-turn). Spec:
#   `ai-storybook-design/api/text-generation/02-narrate-script.md`.
#
# Primary route returns `audio_base64` + `alignment{characters,character_*_times_seconds}`.
# Fallback to `/v1/text-to-speech/{voice_id}` (binary audio, no alignment) only when
# the with-timestamps route 404s with route-not-found (NOT voice-not-found).
# ──────────────────────────────────────────────────────────────────────────────


class ElevenTTSFailedError(ElevenDesignError):
    """Generic upstream 4xx on TTS that doesn't fit a more specific class."""

    def __init__(self, eleven_status: int, body: str) -> None:
        super().__init__(
            f"ElevenLabs TTS failed ({eleven_status}): {body[:200]}"
        )
        self.eleven_status = eleven_status
        self.body = body


class ElevenContentRejectedError(ElevenDesignError):
    """ElevenLabs 422 — safety filter rejected the TTS content."""

    def __init__(self, reason: str = "content rejected") -> None:
        super().__init__(f"ElevenLabs content rejected: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class SingleTurnRawResult:
    """Raw upstream output from TTS-with-timestamps. Handler aggregates words."""

    audio_bytes: bytes
    mime_type: str
    alignment_chars: list[str]
    alignment_start_s: list[float]
    alignment_end_s: list[float]
    has_alignment: bool
    eleven_ms: int
    # Passthrough of upstream `alignment` object (snake_case, seconds unit).
    # NOT a stable contract — mirrors whatever upstream returns.
    raw_alignment: dict[str, Any]
    # True when result came from `/with-timestamps`; False when fallback base
    # TTS returned binary audio without alignment.
    raw_alignment_from_timestamps: bool
    # ⚡ phase-04 cost-hook fields (Đợt 2 logs char-based cost). `char_count` =
    # len(text); `model_id` = the ElevenLabs model used. Added WITH defaults so
    # existing consumers / construction sites stay unbroken (never reordered).
    char_count: int = 0
    model_id: str = ""


_OUTPUT_FORMAT_TO_MIME: dict[str, str] = {
    "mp3_44100_128": "audio/mpeg",
    "mp3_44100_192": "audio/mpeg",
    "mp3_22050_32": "audio/mpeg",
    "pcm_44100": "audio/pcm",
    "pcm_16000": "audio/pcm",
}


def _output_format_to_mime(output_format: str) -> str:
    return _OUTPUT_FORMAT_TO_MIME.get(output_format, "audio/mpeg")


def estimate_cost_usd(char_count: int, model_id: str) -> float:
    """`meta.costEstimate` — delegates to the single AI-usage pricing source
    (ADR-050). Value unchanged (char_rate); an unknown model → 0.0 (a NULL cost
    from `compute_cost` maps to 0.0 to preserve the numeric contract)."""
    return compute_cost("elevenlabs", model_id, {"characters": char_count})["costUsd"] or 0.0


def _eleven_request_id(resp: "httpx.Response | None") -> str | None:
    """Pull the `request-id` response header (provider_request_id) if present."""
    if resp is None:
        return None
    try:
        return resp.headers.get("request-id")
    except Exception:  # noqa: BLE001 — defensive; a header read must never break logging
        return None


def _log_elevenlabs_call(
    *,
    ctx: AiCallContext | None,
    operation: str,
    model_id: str,
    char_count: int,
    request_payload: dict,
    provider_request_id: str | None = None,
    status: str = "success",
    error: object = None,
    ref_blobs: tuple = (),
    output_blobs: tuple = (),
) -> None:
    """Fire-and-forget one `ai_service_logs` row for an ElevenLabs HTTP call
    (ADR-050). Each call fn = one row (`usage_unit='characters'`,
    `usage_amount=char_count`). Audio has no Illustration Entry → LOG-ONLY (no
    `ai_request_id` is surfaced in any response).

    `ref_blobs` (optional `((bytes|url, mime), ...)`) persists any file INPUT
    content-addressed into `request.ref_files` (IVC voice-clone samples). Text-only
    calls (TTS/SFX/music) pass `()` (no-op).

    Cost is billed ONLY on the success path AND only for char-priced TTS models
    (parity with Replicate/Gemini: an error row keeps usage for observability but
    NULL cost — provider did not charge). SFX/music/IVC are NOT char-priced
    (`model_id` absent from `ELEVENLABS_CHAR_PRICING`) → cost None WITHOUT the
    per-call `pricing_unknown` warn (they are intentionally outside the char cost
    model, not a missing price to fill)."""
    cost = (
        compute_cost("elevenlabs", model_id, {"characters": char_count})
        if status == "success" and model_id in ELEVENLABS_CHAR_PRICING
        else None
    )
    log_ai_request(
        AiLogEntry(
            id=new_request_id(), provider="elevenlabs", operation=operation,
            model=model_id, status=status, context=ctx or AiCallContext(),
            usage_unit="characters", usage_amount=char_count,
            provider_request_id=provider_request_id,
            cost=cost,
            request=sanitize_request(request_payload),
            error=(str(error)[:2000] if error is not None else None),
            ref_blobs=ref_blobs,
            output_blobs=output_blobs,
        )
    )


def _classify_eleven_error(
    resp: httpx.Response, voice_id: str
) -> ElevenDesignError:
    """Map ElevenLabs error response → typed exception. Used by both
    `/with-timestamps` and base TTS fallback paths."""
    status = resp.status_code
    try:
        body_text = resp.text
    except Exception:  # noqa: BLE001 — defensive
        body_text = ""
    body_lower = body_text.lower()

    if status in (401, 403):
        return ElevenAuthFailedError()
    if status == 429:
        return ElevenRateLimitedError("ElevenLabs rate limited")
    if 500 <= status < 600:
        return ElevenUpstreamError(eleven_status=status)

    # 400/404/422 heuristics — ElevenLabs surfaces structured detail.
    try:
        body_json = resp.json()
    except ValueError:
        body_json = None
    if isinstance(body_json, dict):
        detail = body_json.get("detail")
        if isinstance(detail, dict):
            code = (detail.get("code") or "").lower()
            msg = (detail.get("message") or "").lower()
            if (
                code == "voice_not_found"
                or "voice_not_found" in msg
                or "voice not found" in msg
            ):
                return ElevenVoiceNotFoundError(
                    detail.get("voice_id") or voice_id
                )
            if code in ("content_safety", "safety_violation") or "safety" in msg:
                return ElevenContentRejectedError(reason=msg or "safety filter")
        elif isinstance(detail, str):
            if (
                "voice_not_found" in detail.lower()
                or "voice not found" in detail.lower()
            ):
                return ElevenVoiceNotFoundError(voice_id)

    if "voice_not_found" in body_lower or "voice not found" in body_lower:
        return ElevenVoiceNotFoundError(voice_id)
    if status == 422:
        return ElevenContentRejectedError(
            reason=body_text[:120] or "content rejected"
        )

    return ElevenTTSFailedError(eleven_status=status, body=body_text)


def _is_route_not_found_404(resp: httpx.Response) -> bool:
    """Distinguish '/with-timestamps route doesn't exist on this account/region'
    (404) from 'voice not found' (404 with detail.code=voice_not_found).

    Returns True only for route-level 404s, where falling back to base TTS makes
    sense. False for voice-not-found 404s — that should propagate as an error.
    """
    if resp.status_code != 404:
        return False
    try:
        body_json = resp.json()
    except ValueError:
        return True  # 404 without parseable body — assume route miss
    if not isinstance(body_json, dict):
        return True
    detail = body_json.get("detail")
    if isinstance(detail, dict):
        code = (detail.get("code") or "").lower()
        msg = (detail.get("message") or "").lower()
        if code == "voice_not_found" or "voice_not_found" in msg:
            return False
    elif isinstance(detail, str):
        if "voice_not_found" in detail.lower() or "voice not found" in detail.lower():
            return False
    return True


def _build_voice_settings(
    stability: float,
    similarity_boost: float,
    style: float,
    speed: float,
) -> dict[str, Any]:
    return {
        "stability": stability,
        "similarity_boost": similarity_boost,
        "style": style,
        "speed": speed,
    }


async def _fallback_base_tts(
    voice_id: str,
    text: str,
    model_id: str,
    voice_settings: dict[str, Any],
    output_format: str,
    seed: int | None,
    headers: dict[str, str],
) -> SingleTurnRawResult:
    """Fallback when `/with-timestamps` route returns 404. Returns audio bytes
    with empty alignment shell — handler will linear-fallback word timing."""
    url = f"{ELEVEN_API_BASE}{ELEVEN_TTS_BASE_PATH_TMPL.format(voice_id=voice_id)}"
    params = {"output_format": output_format}
    payload: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
        "apply_text_normalization": "auto",
    }
    if seed is not None:
        payload["seed"] = seed

    fallback_headers = dict(headers)
    fallback_headers["Accept"] = "audio/mpeg"

    logger.warning(
        "elevenlabs_tts_fallback_base voice_id=%s output_format=%s",
        voice_id, output_format,
    )

    await acquire_eleven_slot("tts_fallback")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_TTS_TIMEOUT_S) as client:
            response = await client.post(
                url, json=payload, params=params, headers=fallback_headers
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_tts_fallback_base_timeout elapsed_s=%.1f",
            time.monotonic() - t0,
        )
        raise ElevenTimeoutError("TTS fallback request timed out") from exc

    eleven_ms = int((time.monotonic() - t0) * 1000)

    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_tts_fallback_http_error status=%d elapsed_ms=%d body_excerpt=%s",
            response.status_code, eleven_ms, response.text[:200],
        )
        raise _classify_eleven_error(response, voice_id)

    audio_bytes = response.content
    if not audio_bytes:
        raise ElevenTTSFailedError(
            eleven_status=response.status_code,
            body="empty audio body from base TTS",
        )

    empty_alignment: dict[str, Any] = {
        "characters": [],
        "character_start_times_seconds": [],
        "character_end_times_seconds": [],
    }

    logger.info(
        "elevenlabs_tts_fallback_done voice_id=%s eleven_ms=%d audio_bytes=%d",
        voice_id, eleven_ms, len(audio_bytes),
    )

    return SingleTurnRawResult(
        audio_bytes=audio_bytes,
        mime_type=_output_format_to_mime(output_format),
        alignment_chars=[],
        alignment_start_s=[],
        alignment_end_s=[],
        has_alignment=False,
        eleven_ms=eleven_ms,
        raw_alignment=empty_alignment,
        raw_alignment_from_timestamps=False,
        char_count=len(text),
        model_id=model_id,
    )


@traceable(
    name="elevenlabs_tts_with_timestamps",
    process_inputs=_strip_settings_from_trace,
)
async def text_to_speech_with_timestamps(
    voice_id: str,
    text: str,
    model_id: str,
    stability: float,
    similarity_boost: float,
    style: float,
    speed: float,
    output_format: str,
    settings: Settings,
    seed: int | None = None,
    ai_context: AiCallContext | None = None,
) -> SingleTurnRawResult:
    """POST ElevenLabs single-turn TTS with char-level alignment timestamps.

    Falls back to `/v1/text-to-speech/{voice_id}` (binary audio, no alignment)
    only when the `/with-timestamps` route 404s with route-not-found. Other
    errors (voice not found, content safety, rate limits, auth) propagate as
    typed exceptions.

    `text` SHOULD include audio tags `[...]` so v3 prosody is preserved.
    Caller is responsible for stripping tags before computing word offsets.
    """
    voice_settings = _build_voice_settings(
        stability, similarity_boost, style, speed
    )
    payload: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
        "apply_text_normalization": "auto",
    }
    if seed is not None:
        payload["seed"] = seed

    url = f"{ELEVEN_API_BASE}{ELEVEN_TTS_TIMESTAMPS_PATH_TMPL.format(voice_id=voice_id)}"
    params = {"output_format": output_format}
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    char_count = len(text)
    logger.info(
        "elevenlabs_tts_start voice_id=%s chars=%d model=%s output_format=%s seed=%s",
        voice_id, char_count, model_id, output_format,
        "set" if seed is not None else "none",
    )
    _attach_run_metadata(
        {
            "eleven_request_payload": {
                "voice_id": voice_id,
                "char_count": char_count,
                "model_id": model_id,
                "output_format": output_format,
                "voice_settings": voice_settings,
                "apply_text_normalization": "auto",
                "seed_set": seed is not None,
            }
        }
    )

    await acquire_eleven_slot("tts")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_TTS_TIMEOUT_S) as client:
            response = await client.post(
                url, json=payload, params=params, headers=headers
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_tts_timeout elapsed_s=%.1f", time.monotonic() - t0
        )
        raise ElevenTimeoutError("TTS request timed out") from exc

    eleven_ms = int((time.monotonic() - t0) * 1000)

    if response.status_code == 404 and _is_route_not_found_404(response):
        logger.warning(
            "elevenlabs_tts_route_404_fallback voice_id=%s body_excerpt=%s",
            voice_id, response.text[:200],
        )
        return await _fallback_base_tts(
            voice_id=voice_id,
            text=text,
            model_id=model_id,
            voice_settings=voice_settings,
            output_format=output_format,
            seed=seed,
            headers=headers,
        )

    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_tts_http_error voice_id=%s status=%d elapsed_ms=%d body_excerpt=%s",
            voice_id, response.status_code, eleven_ms, response.text[:200],
        )
        _log_elevenlabs_call(
            ctx=ai_context, operation="voice.tts_with_timestamps", model_id=model_id,
            char_count=char_count, request_payload=payload,
            provider_request_id=_eleven_request_id(response),
            status="error", error=f"HTTP {response.status_code}: {response.text[:200]}",
        )
        raise _classify_eleven_error(response, voice_id)

    try:
        body = response.json()
    except ValueError as exc:
        raise ElevenTTSFailedError(
            eleven_status=response.status_code,
            body=f"non-JSON response: {response.text[:200]}",
        ) from exc

    audio_b64 = body.get("audio_base64") or body.get("audio")
    if not isinstance(audio_b64, str) or not audio_b64:
        raise ElevenTTSFailedError(
            eleven_status=response.status_code,
            body="missing audio_base64 in response",
        )

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except (ValueError, TypeError) as exc:
        raise ElevenTTSFailedError(
            eleven_status=response.status_code,
            body=f"invalid base64 audio: {exc}",
        ) from exc

    alignment_raw = body.get("alignment")
    alignment = alignment_raw if isinstance(alignment_raw, dict) else {}
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    has_alignment = (
        bool(chars) and len(chars) == len(starts) == len(ends)
    )
    raw_alignment_payload: dict[str, Any] = (
        dict(alignment)
        if alignment
        else {
            "characters": [],
            "character_start_times_seconds": [],
            "character_end_times_seconds": [],
        }
    )

    if not has_alignment:
        logger.warning(
            "elevenlabs_tts_no_alignment voice_id=%s — handler will fallback linear",
            voice_id,
        )

    logger.info(
        "elevenlabs_tts_done voice_id=%s eleven_ms=%d align_chars=%d has_align=%s audio_bytes=%d",
        voice_id, eleven_ms, len(chars), has_alignment, len(audio_bytes),
    )

    _log_elevenlabs_call(
        ctx=ai_context, operation="voice.tts_with_timestamps", model_id=model_id,
        char_count=char_count, request_payload=payload,
        provider_request_id=_eleven_request_id(response),
        output_blobs=(audio_bytes,),  # raw audio → ai-logs/outputs (persisted in insert thread)
    )

    return SingleTurnRawResult(
        audio_bytes=audio_bytes,
        mime_type=_output_format_to_mime(output_format),
        alignment_chars=list(chars),
        alignment_start_s=[float(s) for s in starts],
        alignment_end_s=[float(e) for e in ends],
        has_alignment=has_alignment,
        eleven_ms=eleven_ms,
        raw_alignment=raw_alignment_payload,
        raw_alignment_from_timestamps=has_alignment,
        char_count=char_count,
        model_id=model_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /v1/sound-generation — single-shot SFX generation (binary audio response)
# Consumed by `/api/text/generate-sound-effect`. Spec:
#   `ai-storybook-design/api/text-generation/05-generate-sound-effect.md`.
#
# Unlike TTS-with-timestamps, the success body is raw binary audio (mp3 or
# pcm). Errors stay JSON `{"detail": {...}}` at 4xx/5xx.
# ──────────────────────────────────────────────────────────────────────────────


class ElevenSfxDurationError(ElevenDesignError):
    """ElevenLabs 422 — duration param rejected (out of supported range)."""

    def __init__(self, body: str) -> None:
        super().__init__(f"ElevenLabs SFX duration rejected: {body[:200]}")
        self.body = body


class ElevenSfxFailedError(ElevenDesignError):
    """Generic upstream failure for SFX that doesn't fit a more specific class."""

    def __init__(self, eleven_status: int, body: str) -> None:
        super().__init__(
            f"ElevenLabs SFX failed ({eleven_status}): {body[:200]}"
        )
        self.eleven_status = eleven_status
        self.body = body


@dataclass(frozen=True)
class SoundEffectRawResult:
    """Raw upstream output from /v1/sound-generation."""

    audio_bytes: bytes
    content_type: str  # "audio/mpeg" or "audio/wav"
    eleven_ms: int
    # ⚡ phase-04 cost-hook fields (Đợt 2). `char_count` = len(description).
    # `/v1/sound-generation` takes no caller `model_id`, so `model_id` stays ""
    # (default) — kept for a uniform cost-hook shape across ElevenLabs results.
    # Added WITH defaults so existing consumers stay unbroken.
    char_count: int = 0
    model_id: str = ""


def _classify_sfx_error(resp: httpx.Response) -> ElevenDesignError:
    """Map ElevenLabs SFX error response → typed exception."""
    status = resp.status_code
    try:
        body_text = resp.text
    except Exception:  # noqa: BLE001 — defensive
        body_text = ""
    body_lower = body_text.lower()

    # Try to extract structured detail.message for more precise classification.
    msg_lower = body_lower
    try:
        body_json = resp.json()
    except ValueError:
        body_json = None
    if isinstance(body_json, dict):
        detail = body_json.get("detail")
        if isinstance(detail, dict):
            msg_lower = (detail.get("message") or "").lower() or body_lower
        elif isinstance(detail, str):
            msg_lower = detail.lower() or body_lower

    if status in (401, 403):
        return ElevenAuthFailedError()
    if status == 429:
        return ElevenRateLimitedError("ElevenLabs rate limited")
    if 500 <= status < 600:
        return ElevenUpstreamError(eleven_status=status)
    if status == 422:
        if "duration" in msg_lower:
            return ElevenSfxDurationError(body=body_text)
        if any(kw in msg_lower for kw in ("safety", "content", "moderat")):
            return ElevenContentRejectedError(reason=msg_lower or "safety filter")
    return ElevenSfxFailedError(eleven_status=status, body=body_text)


def _sfx_content_type(output_format: str) -> str:
    return "audio/mpeg" if output_format.startswith("mp3") else "audio/wav"


@traceable(name="elevenlabs_sfx_generate", process_inputs=_strip_settings_from_trace)
async def generate_sound_effect(
    description: str,
    loop: bool,
    duration_secs: float | None,
    prompt_influence: float,
    output_format: str,
    seed: int | None,
    settings: Settings,
    ai_context: AiCallContext | None = None,
) -> SoundEffectRawResult:
    """POST ElevenLabs /v1/sound-generation. Returns raw binary audio.

    Raises ElevenDesignError subclasses on failure (router maps to HTTP).
    """
    body: dict[str, Any] = {
        "text": description,
        "loop": loop,
        "prompt_influence": prompt_influence,
        "output_format": output_format,
    }
    if duration_secs is not None:
        body["duration_seconds"] = duration_secs
    if seed is not None:
        body["seed"] = seed

    accept = "audio/mpeg" if output_format.startswith("mp3") else "*/*"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": accept,
    }

    _attach_run_metadata({"eleven_request_payload": body})

    char_count = len(description)
    logger.info(
        "elevenlabs_sfx_start chars=%d output_format=%s duration_secs=%s "
        "loop=%s seed_set=%s prompt_influence=%.2f",
        char_count, output_format,
        f"{duration_secs:.2f}" if duration_secs is not None else "auto",
        loop, seed is not None, prompt_influence,
    )

    await acquire_eleven_slot("sound_effect")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_SFX_TIMEOUT_S) as client:
            response = await client.post(
                f"{ELEVEN_API_BASE}{ELEVEN_SFX_PATH}",
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_sfx_timeout elapsed_s=%.1f", time.monotonic() - t0,
        )
        raise ElevenTimeoutError("SFX request timed out") from exc

    eleven_ms = int((time.monotonic() - t0) * 1000)

    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_sfx_http_error status=%d elapsed_ms=%d body_excerpt=%s",
            response.status_code, eleven_ms, response.text[:200],
        )
        _log_elevenlabs_call(
            ctx=ai_context, operation="voice.sound_effect", model_id="eleven_sound_generation",
            char_count=char_count, request_payload=body,
            provider_request_id=_eleven_request_id(response),
            status="error", error=f"HTTP {response.status_code}: {response.text[:200]}",
        )
        raise _classify_sfx_error(response)

    audio_bytes = response.content
    if not audio_bytes:
        raise ElevenSfxFailedError(
            eleven_status=response.status_code,
            body="empty audio body",
        )

    content_type = _sfx_content_type(output_format)
    logger.info(
        "elevenlabs_sfx_done eleven_ms=%d audio_bytes=%d content_type=%s",
        eleven_ms, len(audio_bytes), content_type,
    )
    _log_elevenlabs_call(
        ctx=ai_context, operation="voice.sound_effect", model_id="eleven_sound_generation",
        char_count=char_count, request_payload=body,
        provider_request_id=_eleven_request_id(response),
        output_blobs=(audio_bytes,),  # raw SFX audio → ai-logs/outputs
    )
    return SoundEffectRawResult(
        audio_bytes=audio_bytes,
        content_type=content_type,
        eleven_ms=eleven_ms,
        char_count=char_count,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /v1/music/compose — single-shot music generation (binary audio response)
# Consumed by `/api/text/generate-music`. Spec:
#   `ai-storybook-design/api/text-generation/06-generate-music.md`.
#
# Implementation note: `/v1/music/compose` returns raw binary audio when the
# Accept header matches the requested output format (parity with SFX). The
# multipart variant (`/v1/music/compose-detailed`) is only needed when the
# caller needs `song_id` — which v1 spec marks as "reserved for future
# inpaint", and the validation log accepts `meta.songId = null`. Going
# binary keeps the dep surface aligned with SFX (raw httpx, no SDK).
# ──────────────────────────────────────────────────────────────────────────────


class ElevenMusicPaymentRequiredError(ElevenDesignError):
    """ElevenLabs 402 — subscription out of credits."""

    def __init__(self, body: str = "") -> None:
        super().__init__("ElevenLabs out of credits")
        self.body = body


class ElevenMusicContentRejectedError(ElevenDesignError):
    """ElevenLabs 422 — copyright / safety filter rejected the prompt."""

    def __init__(self, reason: str = "content rejected") -> None:
        super().__init__(f"ElevenLabs music content rejected: {reason}")
        self.reason = reason


class ElevenMusicDurationOutOfRangeError(ElevenDesignError):
    """ElevenLabs 4xx — duration param rejected (out of supported range)."""

    def __init__(self, body: str) -> None:
        super().__init__(f"ElevenLabs music duration rejected: {body[:200]}")
        self.body = body


class ElevenMusicGenerateFailedError(ElevenDesignError):
    """Generic upstream 4xx for music compose that doesn't fit a more
    specific class (invalid model_id, bad params, etc.)."""

    def __init__(self, eleven_status: int, body: str) -> None:
        super().__init__(
            f"ElevenLabs music compose failed ({eleven_status}): {body[:200]}"
        )
        self.eleven_status = eleven_status
        self.body = body


class ElevenMusicRateLimitedError(ElevenDesignError):
    """ElevenLabs 429 — quota / concurrency cap on music compose."""


class ElevenMusicAuthFailedError(ElevenDesignError):
    """ElevenLabs 401/403 on music compose — service-side API key misconfig."""

    def __init__(self) -> None:
        super().__init__("ElevenLabs music auth failed (check ELEVENLABS_API_KEY)")


class ElevenMusicUpstreamError(ElevenDesignError):
    """ElevenLabs 5xx on music compose."""

    def __init__(self, eleven_status: int) -> None:
        super().__init__(
            f"ElevenLabs music upstream error ({eleven_status})"
        )
        self.eleven_status = eleven_status


class ElevenMusicTimeoutError(ElevenDesignError):
    """Music compose request exceeded ELEVEN_MUSIC_TIMEOUT_S."""


class ElevenMusicInternalError(ElevenDesignError):
    """Internal failure parsing/handling the music compose response
    (e.g., empty body, decode failure). Distinct from upstream errors so
    handler maps to 500 INTERNAL_ERROR not 502."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Music compose internal failure: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class MusicComposeRawResult:
    """Raw upstream output from /v1/music/compose."""

    audio_bytes: bytes
    content_type: str  # "audio/mpeg" or "audio/wav"
    song_id: str | None  # None for binary path; reserved for compose-detailed
    eleven_ms: int
    # ⚡ phase-04 cost-hook fields (Đợt 2). `char_count` = len(prompt);
    # `model_id` = the compose model used. Added WITH defaults so existing
    # consumers / construction sites stay unbroken.
    char_count: int = 0
    model_id: str = ""


def _classify_music_error(resp: httpx.Response) -> ElevenDesignError:
    """Map ElevenLabs music-compose error response → typed exception."""
    status = resp.status_code
    try:
        body_text = resp.text
    except Exception:  # noqa: BLE001 — defensive
        body_text = ""
    body_lower = body_text.lower()

    msg_lower = body_lower
    try:
        body_json = resp.json()
    except ValueError:
        body_json = None
    if isinstance(body_json, dict):
        detail = body_json.get("detail")
        if isinstance(detail, dict):
            msg_lower = (detail.get("message") or "").lower() or body_lower
        elif isinstance(detail, str):
            msg_lower = detail.lower() or body_lower

    if status in (401, 403):
        return ElevenMusicAuthFailedError()
    if status == 402:
        return ElevenMusicPaymentRequiredError(body=body_text[:200])
    if status == 429:
        return ElevenMusicRateLimitedError("ElevenLabs rate limited")
    if 500 <= status < 600:
        return ElevenMusicUpstreamError(eleven_status=status)
    if status in (400, 422):
        if "duration" in msg_lower or "music_length" in msg_lower:
            return ElevenMusicDurationOutOfRangeError(body=body_text)
        if any(
            kw in msg_lower
            for kw in (
                "copyright",
                "safety",
                "moderat",
                "explicit",
                "content",
            )
        ):
            return ElevenMusicContentRejectedError(
                reason=msg_lower[:120] or "safety filter"
            )
    return ElevenMusicGenerateFailedError(
        eleven_status=status, body=body_text
    )


def _music_content_type(output_format: str) -> str:
    return "audio/mpeg" if output_format.startswith("mp3") else "audio/wav"


@traceable(
    name="elevenlabs_music_compose",
    process_inputs=_strip_settings_from_trace,
)
async def compose_music(
    *,
    prompt: str,
    model_id: str,
    music_length_ms: int | None,
    force_instrumental: bool,
    output_format: str,
    seed: int | None,
    settings: Settings,
    ai_context: AiCallContext | None = None,
) -> MusicComposeRawResult:
    """POST ElevenLabs /v1/music/compose. Returns raw binary audio.

    Raises ElevenDesignError subclasses on failure (router maps to HTTP).
    `song_id` is None on this binary path — caller must accept Optional in meta.
    """
    body: dict[str, Any] = {
        "prompt": prompt,
        "model_id": model_id,
        "force_instrumental": force_instrumental,
        "output_format": output_format,
    }
    if music_length_ms is not None:
        body["music_length_ms"] = music_length_ms
    # `seed` is intentionally NOT sent upstream: ElevenLabs `/v1/music/compose`
    # rejects (422 "`seed` cannot be used with `prompt`") — seed is only valid
    # with `composition_plan` (advanced mode, out of scope for v1). Spec line 70
    # documents the param as "best-effort, behavior chưa document rõ"; the
    # FE-side use case (force-new pathKey via `Date.now() & 0xFFFFFFFF`) still
    # works because seed enters the SHA256 path-key — only upstream is unaware.

    accept = "audio/mpeg" if output_format.startswith("mp3") else "*/*"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": accept,
    }

    _attach_run_metadata({"eleven_request_payload": body})

    char_count = len(prompt)
    logger.info(
        "elevenlabs_music_start chars=%d model=%s output_format=%s "
        "music_length_ms=%s force_instrumental=%s seed_set=%s",
        char_count, model_id, output_format,
        music_length_ms if music_length_ms is not None else "auto",
        force_instrumental, seed is not None,
    )

    await acquire_eleven_slot("music")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_MUSIC_TIMEOUT_S) as client:
            response = await client.post(
                f"{ELEVEN_API_BASE}{ELEVEN_MUSIC_PATH}",
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_music_timeout elapsed_s=%.1f", time.monotonic() - t0,
        )
        raise ElevenMusicTimeoutError("Music compose request timed out") from exc

    eleven_ms = int((time.monotonic() - t0) * 1000)

    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_music_http_error status=%d elapsed_ms=%d body_excerpt=%s",
            response.status_code, eleven_ms, response.text[:200],
        )
        _log_elevenlabs_call(
            ctx=ai_context, operation="voice.compose_music", model_id=model_id,
            char_count=char_count, request_payload=body,
            provider_request_id=_eleven_request_id(response),
            status="error", error=f"HTTP {response.status_code}: {response.text[:200]}",
        )
        raise _classify_music_error(response)

    audio_bytes = response.content
    if not audio_bytes:
        raise ElevenMusicInternalError("empty audio body")

    content_type = _music_content_type(output_format)
    logger.info(
        "elevenlabs_music_done eleven_ms=%d audio_bytes=%d content_type=%s",
        eleven_ms, len(audio_bytes), content_type,
    )
    _log_elevenlabs_call(
        ctx=ai_context, operation="voice.compose_music", model_id=model_id,
        char_count=char_count, request_payload=body,
        provider_request_id=_eleven_request_id(response),
        output_blobs=(audio_bytes,),  # raw music audio → ai-logs/outputs
    )
    return MusicComposeRawResult(
        audio_bytes=audio_bytes,
        content_type=content_type,
        song_id=None,
        eleven_ms=eleven_ms,
        char_count=char_count,
        model_id=model_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /v1/voices/add — Instant Voice Cloning (IVC)
# Multipart audio upload → cloned permanent voice. Used by clone-from-human.
# Sibling of save_preview but with a different upstream contract (raw audio file
# vs preview ID). Errors propagate to handler via ElevenIvcFailedError /
# ElevenVoiceLimitError (reused) / ElevenAuthFailedError (reused) / etc.
# ──────────────────────────────────────────────────────────────────────────────


ELEVEN_IVC_PATH = "/v1/voices/add"
ELEVEN_IVC_TIMEOUT_S = 60.0
ELEVEN_TTS_PREVIEW_TIMEOUT_S = 30.0


class ElevenIvcFailedError(ElevenDesignError):
    """ElevenLabs IVC rejected the source audio (too short / quiet / invalid labels).

    Maps to HTTP 422 ELEVEN_IVC_FAILED at router layer.
    """

    def __init__(self, eleven_status: int, body: str) -> None:
        super().__init__(f"IVC failed status={eleven_status} body={body[:200]}")
        self.eleven_status = eleven_status
        self.body = body


class ElevenTtsPreviewFailedError(ElevenDesignError):
    """TTS preview render failed AFTER IVC succeeded — handler must rollback the
    cloned voice via delete_voice().

    Maps to HTTP 502 ELEVEN_TTS_PREVIEW_FAILED at router layer.
    """

    def __init__(self, voice_id: str, eleven_status: int, body: str) -> None:
        super().__init__(
            f"TTS preview failed voice_id={voice_id} status={eleven_status}"
        )
        self.voice_id = voice_id
        self.eleven_status = eleven_status
        self.body = body


@dataclass(frozen=True)
class IvcCreateResult:
    voice_id: str
    eleven_ivc_ms: int
    raw: dict[str, Any]
    # ⚡ phase-04 cost-hook fields (Đợt 2). IVC clones from an AUDIO upload (no
    # text / caller model_id), so both stay at defaults — present only for a
    # uniform cost-hook shape across ElevenLabs results.
    char_count: int = 0
    model_id: str = ""


@dataclass(frozen=True)
class TtsPreviewResult:
    mp3_bytes: bytes
    model_used: str  # "eleven_v3" | "eleven_multilingual_v2"
    eleven_tts_ms: int
    # ⚡ phase-04 cost-hook fields (Đợt 2). `char_count` = len(preview_text);
    # `model_id` mirrors `model_used` (existing field kept, never reordered).
    # Added WITH defaults so existing consumers stay unbroken.
    char_count: int = 0
    model_id: str = ""


def _classify_ivc_error(resp: httpx.Response) -> ElevenDesignError:
    status = resp.status_code
    try:
        body_text = resp.text
    except Exception:  # noqa: BLE001 — defensive
        body_text = ""
    if status in (401, 403):
        return ElevenAuthFailedError()
    if status == 409:
        return ElevenVoiceLimitError(body=body_text[:200])
    if status == 429:
        return ElevenRateLimitedError("ElevenLabs rate limited")
    if 500 <= status < 600:
        return ElevenUpstreamError(eleven_status=status)
    # 400 / 422 / other 4xx → IVC business failure (audio too short, quiet,
    # invalid labels, schema-incompatible).
    return ElevenIvcFailedError(eleven_status=status, body=body_text)


# Heuristic keywords that signal an ElevenLabs schema/voice_settings rejection
# for the v3 model. When matched on a 400/422 body, we retry the TTS preview
# call with `eleven_multilingual_v2` (defensive fallback per spec § Risks).
_V3_SCHEMA_DRIFT_KEYWORDS: tuple[str, ...] = (
    "voice_settings",
    "model_id",
    "model not found",
    "invalid model",
    "unsupported model",
    "schema",
)


def _looks_like_v3_schema_drift(body: str) -> bool:
    lower = body.lower()
    return any(k in lower for k in _V3_SCHEMA_DRIFT_KEYWORDS)


@traceable(name="elevenlabs_ivc_create", process_inputs=_strip_settings_from_trace)
async def ivc_create(
    audio_bytes: bytes,
    filename: str,
    content_type: str,
    name: str,
    description: str,
    labels: dict[str, str],
    settings: Settings,
    ai_context: AiCallContext | None = None,
) -> IvcCreateResult:
    """Clone a voice via ElevenLabs Instant Voice Cloning (multipart upload).

    Args:
        audio_bytes: Source audio (real human recording, ≥ 5s, ≤ 10MB).
        filename: With ext suffix (e.g., "source.mp3"). ElevenLabs may reject
            files lacking a recognizable extension.
        content_type: MIME type (audio/mpeg, audio/wav, audio/mp4, audio/ogg).
        name: Voice library display name (1-80 chars).
        description: Free-text description (may be empty).
        labels: Dict of string labels (gender, age, accent, language, use_case).
        settings: Application settings (xi-api-key source — stripped from trace).

    Returns:
        IvcCreateResult with cloned voice_id + timing.

    Raises:
        ElevenIvcFailedError: 4xx (typically 422 — audio rejected).
        ElevenVoiceLimitError: 409 — library quota exceeded.
        ElevenAuthFailedError: 401/403 — service-side API key issue.
        ElevenRateLimitedError: 429.
        ElevenUpstreamError: 5xx.
        ElevenTimeoutError: ELEVEN_IVC_TIMEOUT_S exceeded.
    """
    url = f"{ELEVEN_API_BASE}{ELEVEN_IVC_PATH}"
    # NOTE: NO Content-Type header — httpx sets multipart boundary automatically.
    headers = {"xi-api-key": settings.elevenlabs_api_key}

    # httpx multipart payload — `files` for binary, `data` for form fields.
    files_payload = {
        "files": (filename, io.BytesIO(audio_bytes), content_type),
    }
    data_payload = {
        "name": name,
        "description": description or "",
        "labels": json.dumps(labels, ensure_ascii=False),
        "remove_background_noise": "true",
    }

    _attach_run_metadata(
        {
            "eleven_request_payload": {
                "name": name,
                "filename": filename,
                "content_type": content_type,
                "audio_bytes": len(audio_bytes),
                "labels": labels,
                "remove_background_noise": True,
            }
        }
    )

    logger.info(
        "elevenlabs_ivc_create_start name_len=%d audio_bytes=%d content_type=%s labels_keys=%s",
        len(name), len(audio_bytes), content_type, sorted(labels.keys()),
    )

    await acquire_eleven_slot("ivc_create")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=ELEVEN_IVC_TIMEOUT_S) as client:
            response = await client.post(
                url, files=files_payload, data=data_payload, headers=headers,
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_ivc_create_timeout elapsed_s=%.1f audio_bytes=%d",
            time.monotonic() - t0, len(audio_bytes),
        )
        raise ElevenTimeoutError("IVC request timed out") from exc

    eleven_ivc_ms = int((time.monotonic() - t0) * 1000)

    _ivc_req = {
        "name": name, "filename": filename, "content_type": content_type,
        "audio_bytes": len(audio_bytes), "labels": labels,
    }
    if response.status_code >= 400:
        logger.warning(
            "elevenlabs_ivc_create_http_error status=%d elapsed_ms=%d body_excerpt=%s",
            response.status_code, eleven_ivc_ms, response.text[:200],
        )
        _log_elevenlabs_call(
            ctx=ai_context, operation="voice.ivc_create", model_id="eleven_ivc",
            char_count=0, request_payload=_ivc_req,
            provider_request_id=_eleven_request_id(response),
            status="error", error=f"HTTP {response.status_code}: {response.text[:200]}",
            ref_blobs=((audio_bytes, content_type),),
        )
        raise _classify_ivc_error(response)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ElevenIvcFailedError(
            eleven_status=response.status_code,
            body=f"non-JSON response: {response.text[:200]}",
        ) from exc

    voice_id = payload.get("voice_id")
    if not isinstance(voice_id, str) or not voice_id:
        raise ElevenIvcFailedError(
            eleven_status=response.status_code,
            body=f"missing voice_id in response: {response.text[:200]}",
        )

    logger.info(
        "elevenlabs_ivc_create_done voice_id=%s eleven_ivc_ms=%d audio_bytes=%d",
        voice_id, eleven_ivc_ms, len(audio_bytes),
    )
    _log_elevenlabs_call(
        ctx=ai_context, operation="voice.ivc_create", model_id="eleven_ivc",
        char_count=0, request_payload=_ivc_req,
        provider_request_id=_eleven_request_id(response),
        ref_blobs=((audio_bytes, content_type),),
    )
    return IvcCreateResult(
        voice_id=voice_id,
        eleven_ivc_ms=eleven_ivc_ms,
        raw=payload,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /v1/text-to-speech/{voice_id} — render preview MP3 with cloned voice
# v3 → v2 fallback on schema drift (defensive: v3 is alpha, body schema changes
# semi-regularly). Returns raw MP3 bytes (no JSON envelope).
# ──────────────────────────────────────────────────────────────────────────────


_TTS_PREVIEW_VOICE_SETTINGS: dict[str, Any] = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
}


async def _tts_preview_attempt(
    voice_id: str,
    text: str,
    model_id: str,
    settings: Settings,
) -> tuple[httpx.Response, int]:
    url = f"{ELEVEN_API_BASE}/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": _TTS_PREVIEW_VOICE_SETTINGS,
        "output_format": "mp3_44100_128",
    }
    await acquire_eleven_slot("tts_preview")
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=ELEVEN_TTS_PREVIEW_TIMEOUT_S) as client:
        resp = await client.post(url, json=body, headers=headers)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return resp, elapsed_ms


@traceable(name="elevenlabs_tts_preview", process_inputs=_strip_settings_from_trace)
async def tts_preview(
    voice_id: str,
    language: str,
    settings: Settings,
    ai_context: AiCallContext | None = None,
) -> TtsPreviewResult:
    """Render a TTS preview MP3 with a freshly cloned voice.

    Strategy: try `eleven_v3` first. On 400/422 with schema-drift keywords,
    retry once with `eleven_multilingual_v2` (defensive — v3 is alpha).

    Args:
        voice_id: ElevenLabs voice ID (from `ivc_create`).
        language: BCP-47-ish code; falls back to en_US for preview text.
        settings: Application settings (xi-api-key source).

    Returns:
        TtsPreviewResult with MP3 bytes + which model was used + timing.

    Raises:
        ElevenTtsPreviewFailedError: Non-recoverable failure post-fallback.
            Handler MUST call delete_voice(voice_id) to compensate.
        ElevenAuthFailedError, ElevenRateLimitedError, ElevenTimeoutError.
    """
    preview_text = _pick_preview_text(language)

    _attach_run_metadata({"voice_id": voice_id, "language": language})

    logger.info(
        "elevenlabs_tts_preview_start voice_id=%s language=%s text_len=%d model=eleven_v3",
        voice_id, language, len(preview_text),
    )

    model_used = "eleven_v3"
    try:
        resp, elapsed_ms = await _tts_preview_attempt(
            voice_id, preview_text, model_used, settings,
        )
    except httpx.TimeoutException as exc:
        logger.warning(
            "elevenlabs_tts_preview_timeout voice_id=%s model=%s",
            voice_id, model_used,
        )
        raise ElevenTimeoutError("TTS preview timed out") from exc

    if resp.status_code in (400, 422):
        body_text = resp.text
        if _looks_like_v3_schema_drift(body_text):
            logger.warning(
                "elevenlabs_tts_preview_v3_schema_drift voice_id=%s status=%d body_excerpt=%s — retrying with eleven_multilingual_v2",
                voice_id, resp.status_code, body_text[:200],
            )
            model_used = "eleven_multilingual_v2"
            try:
                resp, elapsed_ms = await _tts_preview_attempt(
                    voice_id, preview_text, model_used, settings,
                )
            except httpx.TimeoutException as exc:
                logger.warning(
                    "elevenlabs_tts_preview_timeout voice_id=%s model=%s",
                    voice_id, model_used,
                )
                raise ElevenTimeoutError("TTS preview (v2 fallback) timed out") from exc

    _preview_req = {"voice_id": voice_id, "text": preview_text, "model_id": model_used}
    status = resp.status_code
    if status >= 400:
        body_text = resp.text
        logger.warning(
            "elevenlabs_tts_preview_http_error voice_id=%s status=%d model=%s body_excerpt=%s",
            voice_id, status, model_used, body_text[:200],
        )
        _log_elevenlabs_call(
            ctx=ai_context, operation="voice.tts_preview", model_id=model_used,
            char_count=len(preview_text), request_payload=_preview_req,
            provider_request_id=_eleven_request_id(resp),
            status="error", error=f"HTTP {status}: {body_text[:200]}",
        )
        if status in (401, 403):
            raise ElevenAuthFailedError()
        if status == 429:
            raise ElevenRateLimitedError("ElevenLabs rate limited")
        # 4xx + 5xx alike: handler must compensate (delete cloned voice).
        raise ElevenTtsPreviewFailedError(
            voice_id=voice_id, eleven_status=status, body=body_text,
        )

    mp3_bytes = resp.content
    if not mp3_bytes:
        raise ElevenTtsPreviewFailedError(
            voice_id=voice_id,
            eleven_status=status,
            body="empty MP3 body",
        )

    logger.info(
        "elevenlabs_tts_preview_done voice_id=%s model=%s eleven_tts_ms=%d mp3_bytes=%d",
        voice_id, model_used, elapsed_ms, len(mp3_bytes),
    )
    _log_elevenlabs_call(
        ctx=ai_context, operation="voice.tts_preview", model_id=model_used,
        char_count=len(preview_text), request_payload=_preview_req,
        provider_request_id=_eleven_request_id(resp),
        output_blobs=(mp3_bytes,),  # raw preview audio → ai-logs/outputs
    )
    return TtsPreviewResult(
        mp3_bytes=mp3_bytes,
        model_used=model_used,
        eleven_tts_ms=elapsed_ms,
        char_count=len(preview_text),
        model_id=model_used,
    )
