"""ffmpeg/ffprobe wrappers for audio chunk concatenation.

Supports two paths:
- Fast path (`concat_fast`): ffmpeg concat demuxer with `-c copy` (stream-copy,
  ~ms latency). Requires uniform codec / sample-rate / channels / bitrate
  across all input chunks.
- Slow path (`concat_slow`): filter_complex concat + libmp3lame re-encode at
  target bitrate. Used when chunks diverge in any of the above (per
  `decide_fast_path`).

All ffmpeg/ffprobe invocations are async via `asyncio.create_subprocess_exec`
with a hard timeout — `proc.kill()` on expiry. stderr is captured and truncated
to 500 chars before being surfaced via FFmpegError (avoid leaking host paths).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FFPROBE_BIN = "ffprobe"
FFMPEG_BIN = "ffmpeg"
FFMPEG_TIMEOUT_S = 60.0
FFPROBE_TIMEOUT_S = 10.0

_OUTPUT_BITRATE_KBPS: dict[str, int] = {
    "mp3_44100_128": 128,
    "mp3_44100_192": 192,
}
_OUTPUT_SAMPLE_RATE: dict[str, int] = {
    "mp3_44100_128": 44_100,
    "mp3_44100_192": 44_100,
}

__all__ = [
    "AudioProbe",
    "AudioDecodeError",
    "FFmpegError",
    "FFMPEG_TIMEOUT_S",
    "FFPROBE_TIMEOUT_S",
    "probe_audio",
    "decide_fast_path",
    "concat_fast",
    "concat_slow",
]


@dataclass(frozen=True)
class AudioProbe:
    codec: str
    sample_rate: int
    channels: int
    duration_ms: int
    bitrate: int  # bps; 0 if unknown


class AudioDecodeError(Exception):
    """ffprobe failed or returned no audio stream."""


class FFmpegError(Exception):
    """ffmpeg concat/encode subprocess failed or timed out."""


async def probe_audio(path: str) -> AudioProbe:
    """Run ffprobe and parse codec/sample_rate/channels/duration/bitrate.

    Raises AudioDecodeError on probe failure (timeout, non-zero exit, malformed
    JSON, no audio stream).
    """
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=FFPROBE_TIMEOUT_S
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AudioDecodeError(f"ffprobe timeout path={path}") from exc

    if proc.returncode != 0:
        msg = stderr.decode(errors="replace")[:500]
        raise AudioDecodeError(f"ffprobe rc={proc.returncode} stderr={msg}")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AudioDecodeError(f"ffprobe invalid JSON: {exc}") from exc

    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise AudioDecodeError("no audio stream")
    s = streams[0]
    fmt = data.get("format", {}) or {}

    try:
        sample_rate = int(s.get("sample_rate") or 0)
        channels = int(s.get("channels") or 0)
    except (TypeError, ValueError) as exc:
        raise AudioDecodeError(f"ffprobe bad numeric: {exc}") from exc

    duration_s = _safe_float(fmt.get("duration")) or _safe_float(s.get("duration")) or 0.0
    bitrate = int(_safe_float(fmt.get("bit_rate")) or _safe_float(s.get("bit_rate")) or 0)

    return AudioProbe(
        codec=str(s.get("codec_name") or ""),
        sample_rate=sample_rate,
        channels=channels,
        duration_ms=int(duration_s * 1000),
        bitrate=bitrate,
    )


def _safe_float(v: object) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def decide_fast_path(
    probes: list[AudioProbe], output_format: str
) -> tuple[bool, Optional[str]]:
    """Decide concat strategy. Returns (fast, reencode_reason).

    **MP3 always slow path** — independent ElevenLabs encodes per chunk carry
    encoder delay padding (~13ms @44.1kHz from LAME MDCT lookahead) + a Xing/
    LAME info frame each. Stream-copying via `-f concat -c copy` produces
    audible boundary clicks: padding decodes as silence at every chunk join,
    Xing frames of chunks 1..N embed mid-stream as ~26ms silent frames, and
    independent bit-reservoir state breaks MDCT overlap-add at frame seams.
    `filter_complex concat` decodes each input to PCM (ffmpeg honours the LAME
    tag → strips delay/padding accurately), splices sample-accurately, then
    encodes once with a single Xing tag — clean output.

    Fast path is reserved for future PCM/WAV chunk formats (gapless-safe by
    codec — no encoder delay, no embedded headers). The bitrate/sample-rate/
    codec uniformity checks below run only for those non-MP3 cases.
    """
    if output_format in _OUTPUT_BITRATE_KBPS:
        # MP3 output → MP3 input today (narrate-script). Always re-encode.
        return False, "mp3_gapless_unsafe"

    if output_format not in _OUTPUT_SAMPLE_RATE:
        return False, "format_mismatch"

    target_sr = _OUTPUT_SAMPLE_RATE[output_format]

    if len({p.codec for p in probes}) > 1:
        return False, "codec_mismatch"

    sample_rates = {p.sample_rate for p in probes}
    if len(sample_rates) > 1 or target_sr not in sample_rates:
        return False, "format_mismatch"

    if len({p.channels for p in probes}) > 1:
        return False, "format_mismatch"

    return True, None


def _escape_single_quote(s: str) -> str:
    return s.replace("'", "'\\''")


async def concat_fast(
    chunk_paths: list[Path],
    list_file: Path,
    out_path: Path,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> None:
    """Concat with `-f concat -safe 0 -c copy`. Writes list_file as side-effect."""
    lines = [
        f"file '{_escape_single_quote(str(p.resolve()))}'" for p in chunk_paths
    ]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        FFMPEG_BIN, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    await _run_ffmpeg(cmd, timeout_s)


async def concat_slow(
    chunk_paths: list[Path],
    out_path: Path,
    output_format: str,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> None:
    """filter_complex concat + libmp3lame re-encode at target bitrate."""
    target_kbps = _OUTPUT_BITRATE_KBPS[output_format]
    target_sr = _OUTPUT_SAMPLE_RATE[output_format]
    n = len(chunk_paths)

    cmd: list[str] = [FFMPEG_BIN, "-y", "-loglevel", "error"]
    for p in chunk_paths:
        cmd += ["-i", str(p)]
    filter_str = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[outa]"
    cmd += [
        "-filter_complex", filter_str,
        "-map", "[outa]",
        "-c:a", "libmp3lame",
        "-b:a", f"{target_kbps}k",
        "-ar", str(target_sr),
        str(out_path),
    ]
    await _run_ffmpeg(cmd, timeout_s)


async def _run_ffmpeg(cmd: list[str], timeout_s: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FFmpegError(f"ffmpeg timeout after {timeout_s}s") from exc

    if proc.returncode != 0:
        msg = stderr.decode(errors="replace")[:500]
        logger.warning("ffmpeg_failed rc=%d stderr=%s", proc.returncode, msg)
        raise FFmpegError(msg or f"ffmpeg rc={proc.returncode}")
