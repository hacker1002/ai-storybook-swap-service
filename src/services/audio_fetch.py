"""Typed audio-fetch wrapper around `http_fetch.fetch_image_bytes`.

`fetch_image_bytes` (generic) raises `HTTPException` with `IMAGE_FETCH_ERROR`
codes that callers must remap by inspecting the error message. That coupling
is fragile — any rewording inside `http_fetch` quietly breaks the consumer.

This module exposes a narrow `fetch_audio_bytes()` that:

  1. Delegates to `fetch_image_bytes(allowed_mime_prefix="audio/")` for SSRF +
     size + transport-level checks.
  2. Re-checks the audio MIME against an explicit whitelist (ElevenLabs IVC
     only accepts a handful of subtypes; the prefix check in http_fetch lets
     `audio/webm`, `audio/flac`, etc. through).
  3. Translates the generic `HTTPException` into 6 typed exceptions so handler
     code can `except AudioFetchSizeError:` instead of pattern-matching strings.

Wrapper is the single point of brittleness — when `http_fetch.py` wording
changes, only this file needs updating.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from src.services.http_fetch import fetch_image_bytes

logger = logging.getLogger(__name__)

# ElevenLabs IVC accepted subtypes. `audio/x-*` variants kept because some
# CDNs (e.g., Supabase Storage on legacy uploads) serve `audio/x-wav` /
# `audio/x-m4a` instead of the canonical RFC-3003 form.
ALLOWED_AUDIO_SUBTYPES: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/ogg",
        # MediaRecorder default on Chrome/Firefox (opus codec inside webm
        # container). ElevenLabs IVC accepts webm via ffmpeg decode pipeline
        # — same as Dubbing / Voice Isolator endpoints.
        "audio/webm",
    }
)


class AudioFetchError(Exception):
    """Base class for audio-fetch failures. Handler maps subclasses to spec codes."""


class AudioFetchTimeoutError(AudioFetchError):
    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"Audio fetch timed out after {timeout_s:.1f}s")
        self.timeout_s = timeout_s


class AudioFetchSizeError(AudioFetchError):
    def __init__(self, actual_bytes: int | None, limit_bytes: int) -> None:
        super().__init__(
            f"Audio size {actual_bytes if actual_bytes is not None else '?'} > "
            f"{limit_bytes} bytes limit"
        )
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes


class AudioFetchMimeError(AudioFetchError):
    def __init__(
        self,
        actual_content_type: str,
        allowed_subtypes: frozenset[str],
    ) -> None:
        super().__init__(
            f"Unsupported audio content-type: '{actual_content_type}'"
        )
        self.actual_content_type = actual_content_type
        self.allowed_subtypes = allowed_subtypes


class AudioFetchNotFoundError(AudioFetchError):
    def __init__(self, upstream_status: int) -> None:
        super().__init__(f"Source audio not found (upstream {upstream_status})")
        self.upstream_status = upstream_status


class AudioFetchSsrfError(AudioFetchError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"SSRF guard blocked URL: {reason}")
        self.reason = reason


class AudioFetchUpstreamError(AudioFetchError):
    def __init__(self, status: int, body_excerpt: str) -> None:
        super().__init__(f"Upstream {status}: {body_excerpt[:200]}")
        self.status = status
        self.body_excerpt = body_excerpt


def _detail_message(detail: Any) -> str:
    """Extract message field from HTTPException.detail (str | dict shape)."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "")
    return ""


async def fetch_audio_bytes(
    url: str,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    timeout_s: float = 15.0,
) -> tuple[bytes, str]:
    """Fetch audio bytes from a public URL with SSRF + size + MIME guards.

    Args:
        url: Source URL (must pass `validate_public_url` — DNS + private-IP block).
        max_bytes: Hard cap on response body size (default 10MB).
        timeout_s: Per-request timeout.

    Returns:
        Tuple of (audio_bytes, content_type). content_type is normalized to
        lowercase, no parameters.

    Raises:
        AudioFetchTimeoutError, AudioFetchSizeError, AudioFetchMimeError,
        AudioFetchNotFoundError, AudioFetchSsrfError, AudioFetchUpstreamError.
    """
    try:
        audio_bytes, content_type = await fetch_image_bytes(
            url,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            allowed_mime_prefix="audio/",
        )
    except HTTPException as exc:
        # `http_fetch` shapes detail as {"success":False,"error":{"code","message"}}.
        message = _detail_message(exc.detail)
        msg_lower = message.lower()
        status = exc.status_code

        ssrf_keywords = (
            "ssrf",
            "non-public",
            "private",
            "scheme",
            "hostname",
            "blocked hostname",
            "dns resolution",
        )
        if status == 400 and any(k in msg_lower for k in ssrf_keywords):
            raise AudioFetchSsrfError(reason=message) from exc

        if status == 504:
            raise AudioFetchTimeoutError(timeout_s=timeout_s) from exc

        if status == 422:
            # http_fetch encodes several failure modes behind 422
            # IMAGE_FETCH_ERROR. Discriminate by message substring.
            if "exceeds size cap" in msg_lower or "too large" in msg_lower:
                raise AudioFetchSizeError(
                    actual_bytes=None, limit_bytes=max_bytes,
                ) from exc
            if "content-type" in msg_lower:
                # Inspect message to capture the offending content-type for
                # the typed exception payload.
                ct = ""
                if ":" in message:
                    ct = message.split(":", 1)[1].strip()
                raise AudioFetchMimeError(
                    actual_content_type=ct,
                    allowed_subtypes=ALLOWED_AUDIO_SUBTYPES,
                ) from exc
            if "upstream status 404" in msg_lower:
                raise AudioFetchNotFoundError(upstream_status=404) from exc
            if "upstream status" in msg_lower:
                # Generic upstream non-2xx; surface the status if parseable.
                try:
                    upstream = int(msg_lower.rsplit(" ", 1)[-1])
                except ValueError:
                    upstream = 0
                if upstream == 404:
                    raise AudioFetchNotFoundError(upstream_status=404) from exc
                raise AudioFetchUpstreamError(
                    status=upstream, body_excerpt=message,
                ) from exc
            # Default for 422: treat as upstream so handler returns 502.
            raise AudioFetchUpstreamError(
                status=422, body_excerpt=message,
            ) from exc

        # Anything else (400 non-SSRF, 5xx) → generic upstream.
        raise AudioFetchUpstreamError(
            status=status, body_excerpt=message,
        ) from exc

    # Defense in depth: explicit subtype whitelist beyond the prefix match.
    ct_norm = (content_type or "").lower().strip()
    if ct_norm not in ALLOWED_AUDIO_SUBTYPES:
        logger.warning(
            "audio_fetch_subtype_rejected ct=%s url_host=%s",
            ct_norm, _safe_host(url),
        )
        raise AudioFetchMimeError(
            actual_content_type=ct_norm,
            allowed_subtypes=ALLOWED_AUDIO_SUBTYPES,
        )

    return audio_bytes, ct_norm


def _safe_host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname or "?"
    except Exception:  # noqa: BLE001 — defensive
        return "?"
