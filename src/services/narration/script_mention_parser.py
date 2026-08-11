"""Parse single-turn `@voice_id: content` scripts → SingleTurnInput.

Consumed by the narrate-script router. Pure function (no I/O). Typed exceptions
are mapped to HTTP error codes by the router layer:

    ScriptTooLongError                → 400 SCRIPT_TOO_LONG
    MultiTurnNotSupportedError        → 400 MULTI_TURN_NOT_SUPPORTED
    ScriptParseError(reason=*)        → 400 SCRIPT_PARSE_ERROR (details.reason)

Reason taxonomy (details.reason):
    malformed_format            — no @voice_id: mention found
    empty_turn                  — text strip empty
    empty_turn_after_tag_strip  — text becomes empty after audio tag removal
    voice_id_invalid            — voice_id fails alphanumeric/length guard
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "SingleTurnInput",
    "ScriptParseError",
    "ScriptTooLongError",
    "MultiTurnNotSupportedError",
    "parse_single_turn",
    "strip_audio_tags",
    "MAX_SCRIPT_CHARS",
    "SINGLE_TURN_PATTERN",
    "MENTION_COUNT_PATTERN",
    "VOICE_ID_PATTERN",
]


MAX_SCRIPT_CHARS = 3000

# Single-turn match: one `@voice_id:` at start, content runs to end of string.
SINGLE_TURN_PATTERN = re.compile(
    r"^@([A-Za-z0-9_-]{10,40}):\s*(.+)\Z",
    re.DOTALL,
)
# Count mentions with start-of-line anchor — multi-turn detection.
MENTION_COUNT_PATTERN = re.compile(r"^@[A-Za-z0-9_-]{10,40}:", re.MULTILINE)
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,40}$")
# Audio tag tokens like `[excited]` — preserved when sent to ElevenLabs (inline
# prosody cues), stripped from the user-facing response.
_AUDIO_TAG_RE = re.compile(r"\[[^\]]+\]")


class ScriptTooLongError(Exception):
    """Script exceeds MAX_SCRIPT_CHARS. Router → 400 SCRIPT_TOO_LONG."""

    def __init__(self, length: int, limit: int = MAX_SCRIPT_CHARS) -> None:
        super().__init__(f"Script length {length} exceeds max {limit} chars")
        self.length = length
        self.limit = limit


class ScriptParseError(Exception):
    """Structural parse failure. Router → 400 SCRIPT_PARSE_ERROR with details.reason."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or f"Script parse failed: {reason}")
        self.reason = reason
        self.message = message or f"Script parse failed: {reason}"


class MultiTurnNotSupportedError(Exception):
    """≥2 `@voice_id:` mentions detected. Router → 400 MULTI_TURN_NOT_SUPPORTED."""

    def __init__(self, mention_count: int) -> None:
        super().__init__(
            f"Multi-turn dialogue not supported (found {mention_count} mentions)"
        )
        self.mention_count = mention_count


@dataclass(frozen=True)
class SingleTurnInput:
    """Parsed single-turn script.

    `text_with_tags` keeps audio tags inline — sent to ElevenLabs to preserve
    v3 prosody features. `text_clean` is the stripped form returned in the
    response and used as the basis for word-timing offsets.
    """

    voice_id: str
    text_with_tags: str
    text_clean: str


def strip_audio_tags(text: str) -> str:
    """Remove `[tag]` markers and collapse leftover whitespace runs."""
    cleaned = _AUDIO_TAG_RE.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_single_turn(script: str) -> SingleTurnInput:
    """Parse a single-turn script into a SingleTurnInput.

    Raises:
        ScriptTooLongError: `script` exceeds MAX_SCRIPT_CHARS.
        MultiTurnNotSupportedError: ≥2 mentions detected.
        ScriptParseError: malformed input — see `reason` for taxonomy.
    """
    length = len(script)
    if length > MAX_SCRIPT_CHARS:
        raise ScriptTooLongError(length=length)

    trimmed = script.strip()
    if not trimmed:
        raise ScriptParseError(
            reason="malformed_format",
            message="Script is empty",
        )

    mention_count = len(MENTION_COUNT_PATTERN.findall(trimmed))
    if mention_count == 0:
        raise ScriptParseError(
            reason="malformed_format",
            message="No @voice_id: mention found in script",
        )
    if mention_count >= 2:
        raise MultiTurnNotSupportedError(mention_count=mention_count)

    m = SINGLE_TURN_PATTERN.match(trimmed)
    if not m:
        raise ScriptParseError(
            reason="malformed_format",
            message="Script does not match @voice_id: <content> pattern",
        )

    voice_id = m.group(1)
    if not VOICE_ID_PATTERN.match(voice_id):
        # Defensive — outer regex already enforces format.
        raise ScriptParseError(
            reason="voice_id_invalid",
            message=f"Invalid voice_id format: {voice_id[:40]}",
        )

    text_with_tags = m.group(2).strip()
    if not text_with_tags:
        raise ScriptParseError(
            reason="empty_turn",
            message="Turn content is empty",
        )

    text_clean = strip_audio_tags(text_with_tags)
    if not text_clean:
        raise ScriptParseError(
            reason="empty_turn_after_tag_strip",
            message="Turn content is empty after stripping audio tags",
        )

    return SingleTurnInput(
        voice_id=voice_id,
        text_with_tags=text_with_tags,
        text_clean=text_clean,
    )
