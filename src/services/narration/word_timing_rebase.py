"""Rebase per-chunk word timings to combined-track absolute offsets.

Pure function — no I/O. Computes:
- chunkOffsetsMs (cumsum of chunk durations)
- joinedScript (separator-joined per-chunk script)
- per-word startMs/endMs shifted by chunk offset
- per-word charStart/charEnd shifted by joined-script char offset

Soft-handles drift: if `endMs > total_duration_ms`, log + clamp (do not fail).
Soft-handles non-monotonic startMs across chunks (rare boundary edge): log
WARN counter, do not raise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DRIFT_TOLERANCE_MS = 50  # spec § Constants

__all__ = [
    "RebasedWord",
    "RebaseResult",
    "rebase_word_timings",
]


@dataclass(frozen=True)
class RebasedWord:
    text: str
    start_ms: int
    end_ms: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class RebaseResult:
    words: list[RebasedWord]
    chunk_offsets_ms: list[int]
    joined_script: str
    duration_ms: int
    drift_clamps: int
    monotonic_warns: int


def rebase_word_timings(
    chunks,  # list of ChunkInput-like (must have .script + .word_timings)
    chunk_durations_ms: list[int],
    separator: str,
) -> RebaseResult:
    n = len(chunks)
    if n == 0:
        return RebaseResult([], [], "", 0, 0, 0)
    if len(chunk_durations_ms) != n:
        raise ValueError(
            f"chunk_durations_ms length {len(chunk_durations_ms)} != chunks {n}"
        )

    chunk_offsets: list[int] = [0] * n
    for i in range(1, n):
        chunk_offsets[i] = chunk_offsets[i - 1] + chunk_durations_ms[i - 1]
    duration_ms = chunk_offsets[-1] + chunk_durations_ms[-1]

    cum_script: list[int] = [0] * n
    for i in range(1, n):
        cum_script[i] = cum_script[i - 1] + len(chunks[i - 1].script) + len(separator)

    joined_script = separator.join(c.script for c in chunks)

    # Sanity check (don't fail — observability only).
    expected_len = cum_script[-1] + len(chunks[-1].script)
    if len(joined_script) != expected_len:
        logger.error(
            "rebase_joined_len_mismatch got=%d expected=%d",
            len(joined_script), expected_len,
        )

    drift_clamps = 0
    monotonic_warns = 0
    last_start = -1
    words: list[RebasedWord] = []

    for ci, chunk in enumerate(chunks):
        chunk_offset_ms = chunk_offsets[ci]
        chunk_offset_chars = cum_script[ci]
        for w in chunk.word_timings:
            sm = w.start_ms + chunk_offset_ms
            em = w.end_ms + chunk_offset_ms
            if em > duration_ms:
                if em - duration_ms > _DRIFT_TOLERANCE_MS:
                    logger.warning(
                        "rebase_drift_clamp em=%d duration=%d delta=%d chunk=%d",
                        em, duration_ms, em - duration_ms, ci,
                    )
                drift_clamps += 1
                em = duration_ms
            if sm < last_start:
                monotonic_warns += 1
            last_start = sm
            words.append(RebasedWord(
                text=w.text,
                start_ms=sm,
                end_ms=em,
                char_start=w.char_start + chunk_offset_chars,
                char_end=w.char_end + chunk_offset_chars,
            ))

    return RebaseResult(
        words=words,
        chunk_offsets_ms=chunk_offsets,
        joined_script=joined_script,
        duration_ms=duration_ms,
        drift_clamps=drift_clamps,
        monotonic_warns=monotonic_warns,
    )
