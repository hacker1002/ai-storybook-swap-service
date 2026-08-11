"""CJK-aware single-turn word aggregator for ElevenLabs alignment data.

Consumed by `/api/text/narrate-script` (single-turn TTS). Pure function — no I/O.

Strategy:
    1. Tokenize `text_clean` (audio tags already stripped upstream) using a
       CJK-per-codepoint rule + whitespace split for Latin/Vietnamese/Hangul.
    2. Walk alignment_chars with a cursor, greedily matching each token's first
       char. Drift-tolerant: counts misses up to DRIFT_THRESHOLD before
       falling back to linear approximation across the full duration.
    3. Linear fallback distributes word duration proportional to char count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

__all__ = [
    "aggregate_single_turn_words",
    "tokenize_cjk_aware",
    "is_cjk_codepoint",
    "WordSpan",
    "DRIFT_THRESHOLD",
]

logger = logging.getLogger(__name__)


DRIFT_THRESHOLD = 0.3  # >30% token misses → linear fallback


# Per-codepoint tokenization for languages without inter-word whitespace.
# Hangul (0xAC00..0xD7AF) intentionally EXCLUDED — modern Korean uses eojeol
# whitespace, so the default whitespace-split path produces natural tokens.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),    # CJK Symbols and Punctuation
    (0x3040, 0x309F),    # Hiragana
    (0x30A0, 0x30FF),    # Katakana
    (0x31F0, 0x31FF),    # Katakana Phonetic Extensions
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),    # Halfwidth and Fullwidth Forms
    (0x20000, 0x2FFFF),  # CJK Ext B–F (supplementary)
)


@dataclass(frozen=True)
class WordSpan:
    text: str
    start_ms: int
    end_ms: int
    char_start: int
    char_end: int


def is_cjk_codepoint(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def tokenize_cjk_aware(text: str) -> list[tuple[str, int, int]]:
    """Tokenize for word-timing. Returns list of (token, char_start, char_end).

    Rules:
        - Whitespace skipped (no token emitted, offset advances).
        - Each CJK codepoint emits a single-character token.
        - Consecutive non-CJK, non-whitespace chars form one token (Latin words,
          numbers, Vietnamese diacritics, Hangul eojeol).

    Offsets are codepoint indices into `text` (Python str native).
    """
    tokens: list[tuple[str, int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if is_cjk_codepoint(ch):
            tokens.append((ch, i, i + 1))
            i += 1
            continue
        start = i
        while i < n and not text[i].isspace() and not is_cjk_codepoint(text[i]):
            i += 1
        tokens.append((text[start:i], start, i))
    return tokens


def aggregate_single_turn_words(
    text_clean: str,
    alignment_chars: list[str],
    alignment_start_s: list[float],
    alignment_end_s: list[float],
) -> tuple[list[WordSpan], bool]:
    """Build per-word timing from tokenized text + char-level alignment.

    Returns `(words, fallback)`. `fallback=True` means linear approximation
    was used (alignment empty/mismatched, or drift >DRIFT_THRESHOLD).
    """
    tokens = tokenize_cjk_aware(text_clean)
    if not tokens:
        return [], False

    if (
        not alignment_chars
        or not alignment_end_s
        or len(alignment_chars) != len(alignment_start_s)
        or len(alignment_chars) != len(alignment_end_s)
    ):
        logger.warning(
            "narration_align_empty fallback_linear tokens=%d", len(tokens)
        )
        return _linear_fallback(tokens, total_duration_ms=0), True

    total_duration_ms = int(alignment_end_s[-1] * 1000)
    align_n = len(alignment_chars)

    cursor = 0
    drift = 0
    matched: list[WordSpan | None] = []
    for token, cs, ce in tokens:
        token_first = token[0]
        match_idx = _find_match(alignment_chars, cursor, token_first)
        if match_idx is None:
            drift += 1
            matched.append(None)
            continue
        token_len = ce - cs
        match_end = match_idx + token_len
        if match_end > align_n:
            drift += 1
            matched.append(None)
            continue
        start_ms = int(alignment_start_s[match_idx] * 1000)
        end_ms = int(alignment_end_s[match_end - 1] * 1000)
        matched.append(
            WordSpan(
                text=token,
                start_ms=start_ms,
                end_ms=end_ms,
                char_start=cs,
                char_end=ce,
            )
        )
        cursor = match_end

    if drift > len(tokens) * DRIFT_THRESHOLD:
        logger.warning(
            "narration_align_drift fallback_linear tokens=%d drift=%d threshold=%.2f",
            len(tokens), drift, DRIFT_THRESHOLD,
        )
        return _linear_fallback(tokens, total_duration_ms), True

    return _fill_gaps(tokens, matched, total_duration_ms), False


def _find_match(
    alignment_chars: list[str], cursor: int, target_ch: str
) -> int | None:
    """Find first index >= cursor where alignment_chars[i] == target_ch."""
    n = len(alignment_chars)
    for i in range(cursor, n):
        if alignment_chars[i] == target_ch:
            return i
    return None


def _fill_gaps(
    tokens: list[tuple[str, int, int]],
    matched: list[WordSpan | None],
    total_duration_ms: int,
) -> list[WordSpan]:
    """Interpolate timing for tokens that failed alignment match."""
    out: list[WordSpan] = []
    n = len(matched)
    for i, slot in enumerate(matched):
        if slot is not None:
            out.append(slot)
            continue
        token, cs, ce = tokens[i]
        prev_end = 0
        for j in range(i - 1, -1, -1):
            if matched[j] is not None:
                prev_end = matched[j].end_ms
                break
        next_start = total_duration_ms
        for j in range(i + 1, n):
            if matched[j] is not None:
                next_start = matched[j].start_ms
                break
        # Equal split among consecutive unmatched neighbors.
        gap_tokens = 1
        for j in range(i - 1, -1, -1):
            if matched[j] is None:
                gap_tokens += 1
            else:
                break
        for j in range(i + 1, n):
            if matched[j] is None:
                gap_tokens += 1
            else:
                break
        span_ms = max(0, (next_start - prev_end) // max(gap_tokens, 1))
        start_ms = prev_end
        end_ms = prev_end + span_ms
        out.append(
            WordSpan(
                text=token,
                start_ms=start_ms,
                end_ms=end_ms,
                char_start=cs,
                char_end=ce,
            )
        )
    return out


def _linear_fallback(
    tokens: list[tuple[str, int, int]],
    total_duration_ms: int,
) -> list[WordSpan]:
    """Distribute total_duration_ms proportional to per-token char length."""
    if not tokens:
        return []
    total_chars = sum(len(t[0]) for t in tokens) or 1
    cursor_ms = 0
    out: list[WordSpan] = []
    for token, cs, ce in tokens:
        share = len(token) / total_chars
        span_ms = int(total_duration_ms * share) if total_duration_ms else 0
        out.append(
            WordSpan(
                text=token,
                start_ms=cursor_ms,
                end_ms=cursor_ms + span_ms,
                char_start=cs,
                char_end=ce,
            )
        )
        cursor_ms += span_ms
    return out
