"""Audio duration probe for ElevenLabs SFX output.

mp3_*  → mutagen `MP3.info.length` (parses frame headers; works on
         tag-less mp3 ElevenLabs returns).
pcm_44100 → 16-bit mono assumed: `bytes / (44100 * 2)`.
pcm_16000 → 16-bit mono assumed: `bytes / (16000 * 2)`.

Caller should wrap in `await asyncio.to_thread(...)` since mutagen's I/O is
synchronous (small in-memory parse, but still blocks the loop).
"""

from __future__ import annotations

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

__all__ = ["probe_duration", "probe_duration_ms", "AudioDurationProbeError"]


# PCM rate × bytes-per-sample. ElevenLabs `pcm_*` is 16-bit signed LE mono.
_PCM_RATES: dict[str, int] = {
    "pcm_44100": 44100,
    "pcm_16000": 16000,
}
_PCM_BYTES_PER_SAMPLE = 2  # 16-bit


class AudioDurationProbeError(Exception):
    """Raised when audio duration cannot be determined from raw bytes."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Audio duration probe failed: {reason}")
        self.reason = reason


def probe_duration(audio_bytes: bytes, output_format: str) -> float:
    """Return duration in seconds for the given audio bytes + ElevenLabs format.

    Raises `AudioDurationProbeError` on parse failure or unsupported format.
    """
    if not audio_bytes:
        raise AudioDurationProbeError("empty audio bytes")

    if output_format.startswith("mp3"):
        try:
            # Import inline to keep module import lightweight; mutagen is small
            # but pulled lazily for parity with other optional deps.
            from mutagen.mp3 import MP3
        except ImportError as exc:  # pragma: no cover — guarded by pyproject
            raise AudioDurationProbeError(
                f"mutagen unavailable: {exc}"
            ) from exc
        try:
            mp3 = MP3(BytesIO(audio_bytes))
            length = float(mp3.info.length)
        except Exception as exc:  # noqa: BLE001 — mutagen wraps various errors
            logger.warning(
                "audio_probe_mp3_failed format=%s bytes=%d error=%s",
                output_format, len(audio_bytes), exc,
            )
            raise AudioDurationProbeError(
                f"mp3 parse failed: {exc}"
            ) from exc
        if length <= 0:
            raise AudioDurationProbeError(
                f"mp3 length non-positive: {length}"
            )
        return length

    rate = _PCM_RATES.get(output_format)
    if rate is None:
        raise AudioDurationProbeError(
            f"unknown output_format: {output_format}"
        )

    if len(audio_bytes) % _PCM_BYTES_PER_SAMPLE != 0:
        raise AudioDurationProbeError(
            f"pcm bytes ({len(audio_bytes)}) not aligned to "
            f"{_PCM_BYTES_PER_SAMPLE}-byte samples"
        )
    return len(audio_bytes) / (rate * _PCM_BYTES_PER_SAMPLE)


def probe_duration_ms(audio_bytes: bytes, output_format: str) -> int:
    """Wrapper returning duration in integer milliseconds for `meta.durationMs`.

    Caller (music handler) wraps this in `asyncio.to_thread` since the mp3
    branch goes through mutagen.
    """
    return int(probe_duration(audio_bytes, output_format) * 1000)
