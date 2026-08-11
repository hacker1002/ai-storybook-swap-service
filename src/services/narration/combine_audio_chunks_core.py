"""In-process core for POST /api/text/combine-audio-chunks.

Pipeline parity with the router but:
  - Raises `NarrationDomainError` (status/code/message/details) instead of
    HTTPException — callers map to HTTP or job error envelope.
  - Returns a plain dataclass; `words` is a camelCase dict list (JSONB-ready).
  - `@traceable` lives here, so direct callers (job handler) share the trace.

Total wall-clock budget (`TOTAL_TIMEOUT_S = 90`) enforced by router via
`asyncio.wait_for` — keep that responsibility at the boundary so in-process
callers can choose their own budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import HTTPException
from langsmith import traceable

from src.models.requests.text_combine_audio_chunks import CombineAudioChunksRequest
from src.services.narration.audio_concat import (
    FFMPEG_TIMEOUT_S,
    AudioDecodeError,
    FFmpegError,
    concat_fast,
    concat_slow,
    decide_fast_path,
    probe_audio,
)
from src.services.narration.errors import NarrationDomainError
from src.services.narration.word_timing_rebase import rebase_word_timings
# SEAM: image-api's `validate_supabase_url` (Supabase-host allowlist) has NO
# equivalent here — this service's ssrf_guard.py is DELIBERATELY generic
# (validate_public_url + settings.ssrf_allowed_hosts_list env allowlist, see
# its module docstring). validate_public_url is the designed replacement: it
# performs the same private-IP/DNS guard and honors the same local-loopback
# allowlist pattern used everywhere else in this service.
from src.services.ssrf_guard import validate_public_url
from src.services.storage import (
    StorageUploadError,
    build_combined_narration_path,
    upload_bytes,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CombineAudioChunksCoreResult",
    "run_combine_audio_chunks",
    "FETCH_TIMEOUT_S",
    "MAX_PER_CHUNK_BYTES",
    "MAX_TOTAL_BYTES",
]

FETCH_TIMEOUT_S = 30.0
MAX_PER_CHUNK_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024


# ─── Result shape ────────────────────────────────────────────────────────────


from dataclasses import dataclass


@dataclass(frozen=True)
class CombineAudioChunksCoreResult:
    # data
    audio_url: str
    duration_ms: int
    words: list[dict[str, Any]]  # camelCase: {text, startMs, endMs, charStart, charEnd}
    chunk_offsets_ms: list[int]
    joined_script: str

    # meta
    processing_time_ms: int
    download_ms: int
    encode_ms: int
    upload_ms: int
    fast_path: bool
    reencode_reason: Optional[str]
    path_key: str
    chunk_count: int
    total_input_size_bytes: int


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _build_path_key(req: CombineAudioChunksRequest) -> str:
    """SHA256 over canonical (audioUrl, scriptHash, wordTimingsHash) per chunk
    + scriptSeparator + outputFormat. Order-sensitive across chunks
    (intentional — chunk order changes audio output).
    """
    canonical = {
        "chunks": [
            {
                "audioUrl": c.audio_url,
                "scriptHash": hashlib.sha256(c.script.encode("utf-8")).hexdigest(),
                "wordTimingsHash": hashlib.sha256(
                    json.dumps(
                        [
                            {
                                "text": w.text,
                                "startMs": w.start_ms,
                                "endMs": w.end_ms,
                                "charStart": w.char_start,
                                "charEnd": w.char_end,
                            }
                            for w in c.word_timings
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for c in req.chunks
        ],
        "scriptSeparator": req.script_separator,
        "outputFormat": req.output_format,
    }
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _fetch_chunk(idx: int, url: str, dest: Path) -> int:
    """Download one chunk. SSRF-guarded, redirect-disabled (guard runs on the
    original URL only, so any 3xx is treated as upstream failure).

    Raises NarrationDomainError with `details.failedChunkIndex` for caller
    attribution.
    """
    try:
        validate_public_url(url)
    except HTTPException as exc:
        # SSRF guard raises FastAPI HTTPException with the spec error envelope —
        # translate to NarrationDomainError so the service layer stays HTTP-free.
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        err = detail.get("error") if isinstance(detail, dict) else None
        code = (err or {}).get("code", "CHUNK_FETCH_FORBIDDEN")
        message = (err or {}).get("message", "URL blocked by SSRF guard")
        raise NarrationDomainError(
            status=exc.status_code,
            code=code,
            message=message,
            details={"failedChunkIndex": idx},
        ) from exc

    timeout = httpx.Timeout(FETCH_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                if 300 <= resp.status_code < 400:
                    raise NarrationDomainError(
                        status=502,
                        code="CHUNK_FETCH_FAILED",
                        message=f"Unexpected redirect status {resp.status_code}",
                        details={
                            "failedChunkIndex": idx,
                            "status": resp.status_code,
                        },
                    )

                if resp.status_code >= 400:
                    raise NarrationDomainError(
                        status=502,
                        code="CHUNK_FETCH_FAILED",
                        message=f"Upstream status {resp.status_code}",
                        details={
                            "failedChunkIndex": idx,
                            "status": resp.status_code,
                        },
                    )

                ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ct and not ct.startswith("audio/"):
                    logger.warning(
                        "combine_audio_unexpected_mime idx=%d ct=%s", idx, ct
                    )

                size = 0
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PER_CHUNK_BYTES:
                            raise NarrationDomainError(
                                status=400,
                                code="VALIDATION_ERROR",
                                message="Chunk exceeds 50MB cap",
                                details={"failedChunkIndex": idx},
                            )
                        f.write(chunk)
                return size
    except NarrationDomainError:
        raise
    except httpx.TimeoutException as exc:
        raise NarrationDomainError(
            status=504,
            code="TIMEOUT",
            message="Chunk fetch timeout",
            details={"failedChunkIndex": idx},
        ) from exc
    except httpx.HTTPError as exc:
        raise NarrationDomainError(
            status=502,
            code="CHUNK_FETCH_FAILED",
            message=str(exc),
            details={"failedChunkIndex": idx},
        ) from exc


# ─── Entry point ─────────────────────────────────────────────────────────────


@traceable(name="text_combine_audio_chunks")
async def run_combine_audio_chunks(
    request: CombineAudioChunksRequest,
) -> CombineAudioChunksCoreResult:
    t_start = time.monotonic()
    path_key = _build_path_key(request)
    n = len(request.chunks)
    logger.info(
        "combine_audio_start path_key=%s chunk_count=%d output_format=%s",
        path_key[:16], n, request.output_format,
    )

    with tempfile.TemporaryDirectory(prefix="combine-") as tdir:
        tdir_path = Path(tdir)
        chunk_paths = [tdir_path / f"chunk_{i:03d}.mp3" for i in range(n)]
        out_path = tdir_path / "out.mp3"
        list_file = tdir_path / "concat.txt"

        # 1. Parallel fetch ----------------------------------------------------
        t_dl = time.monotonic()
        results = await asyncio.gather(
            *[
                _fetch_chunk(i, c.audio_url, chunk_paths[i])
                for i, c in enumerate(request.chunks)
            ],
            return_exceptions=True,
        )
        sizes: list[int] = []
        for i, r in enumerate(results):
            if isinstance(r, NarrationDomainError):
                raise r
            if isinstance(r, BaseException):
                raise NarrationDomainError(
                    status=502,
                    code="CHUNK_FETCH_FAILED",
                    message=str(r),
                    details={"failedChunkIndex": i},
                ) from r
            sizes.append(int(r))
        total_bytes = sum(sizes)
        if total_bytes > MAX_TOTAL_BYTES:
            raise NarrationDomainError(
                status=400,
                code="VALIDATION_ERROR",
                message="Total audio exceeds 200MB cap",
            )
        download_ms = int((time.monotonic() - t_dl) * 1000)

        # 2. Probe each chunk --------------------------------------------------
        probe_results = await asyncio.gather(
            *[probe_audio(str(p)) for p in chunk_paths],
            return_exceptions=True,
        )
        probes = []
        for i, pr in enumerate(probe_results):
            if isinstance(pr, AudioDecodeError):
                raise NarrationDomainError(
                    status=422,
                    code="AUDIO_DECODE_ERROR",
                    message=str(pr),
                    details={"failedChunkIndex": i},
                ) from pr
            if isinstance(pr, BaseException):
                raise NarrationDomainError(
                    status=422,
                    code="AUDIO_DECODE_ERROR",
                    message=str(pr),
                    details={"failedChunkIndex": i},
                ) from pr
            probes.append(pr)

        # 3. Decide path + concat ---------------------------------------------
        t_enc = time.monotonic()
        fast, reencode_reason = decide_fast_path(probes, request.output_format)
        try:
            if fast:
                await concat_fast(
                    chunk_paths, list_file, out_path, FFMPEG_TIMEOUT_S
                )
            else:
                await concat_slow(
                    chunk_paths, out_path, request.output_format, FFMPEG_TIMEOUT_S
                )
        except FFmpegError as exc:
            raise NarrationDomainError(
                status=500, code="FFMPEG_ERROR", message=str(exc)
            ) from exc
        encode_ms = int((time.monotonic() - t_enc) * 1000)

        # 4. Probe output for accurate duration -------------------------------
        try:
            out_probe = await probe_audio(str(out_path))
        except AudioDecodeError as exc:
            raise NarrationDomainError(
                status=500, code="FFMPEG_ERROR", message="Output unreadable"
            ) from exc

        # 5. Rebase word timings ----------------------------------------------
        chunk_durations = [p.duration_ms for p in probes]
        rebase = rebase_word_timings(
            request.chunks, chunk_durations, request.script_separator
        )
        duration_ms = out_probe.duration_ms or rebase.duration_ms

        # Drift > 50ms signals upstream encoder change (no LAME tag / alt encoder).
        sum_chunks_ms = sum(chunk_durations)
        drift_ms = (out_probe.duration_ms or 0) - sum_chunks_ms
        if abs(drift_ms) > 50:
            logger.warning(
                "combine_duration_drift path_key=%s out_ms=%d sum_chunks_ms=%d drift_ms=%d chunks=%d",
                path_key[:16], out_probe.duration_ms or 0, sum_chunks_ms,
                drift_ms, n,
            )

        # 6. Upload ------------------------------------------------------------
        t_up = time.monotonic()
        audio_bytes = out_path.read_bytes()
        storage_path = build_combined_narration_path(path_key)
        try:
            audio_url = await upload_bytes(
                path=storage_path,
                body=audio_bytes,
                content_type="audio/mpeg",
                upsert=True,
            )
        except StorageUploadError as exc:
            raise NarrationDomainError(
                status=500,
                code="STORAGE_UPLOAD_ERROR",
                message="Failed to persist combined audio",
            ) from exc
        upload_ms = int((time.monotonic() - t_up) * 1000)

        processing_ms = int((time.monotonic() - t_start) * 1000)
        logger.info(
            "combine_audio_done path_key=%s duration_ms=%d fast_path=%s "
            "processing_ms=%d download_ms=%d encode_ms=%d upload_ms=%d "
            "drift_clamps=%d monotonic_warns=%d total_bytes=%d",
            path_key[:16], duration_ms, fast, processing_ms,
            download_ms, encode_ms, upload_ms,
            rebase.drift_clamps, rebase.monotonic_warns, total_bytes,
        )

        return CombineAudioChunksCoreResult(
            audio_url=audio_url,
            duration_ms=duration_ms,
            words=[
                {
                    "text": w.text,
                    "startMs": w.start_ms,
                    "endMs": w.end_ms,
                    "charStart": w.char_start,
                    "charEnd": w.char_end,
                }
                for w in rebase.words
            ],
            chunk_offsets_ms=rebase.chunk_offsets_ms,
            joined_script=rebase.joined_script,
            processing_time_ms=processing_ms,
            download_ms=download_ms,
            encode_ms=encode_ms,
            upload_ms=upload_ms,
            fast_path=fast,
            reencode_reason=reencode_reason,
            path_key=path_key,
            chunk_count=n,
            total_input_size_bytes=total_bytes,
        )
