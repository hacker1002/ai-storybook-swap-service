"""Voice resolution helpers for the remix audio-swap handler + enqueue precheck.

Ported from image-api `src/services/remix_voice_resolver.py`. The pure-logic
helpers (`voice_to_reader`, `resolve_voice_for_chunk`, `needs_regen`,
`iter_enabled_languages`) are byte-identical. The ONLY change is the async DB
seam: image-api's `sb.table("voices")...` Supabase calls become
`get_adapter().list_voices(...)` asyncpg reads (the `voices` table is loaded
globally — no book_id column — so we fetch all rows and filter to the requested
UUIDs, matching image-api's `IN (voice_ids)` semantics).
"""

from __future__ import annotations

import logging
from typing import Iterable

from src.db.adapter import get_adapter

logger = logging.getLogger(__name__)

__all__ = [
    "voice_to_reader",
    "resolve_voice_for_chunk",
    "needs_regen",
    "iter_enabled_languages",
    "lookup_eleven_id",
    "build_eleven_id_cache",
]


def voice_to_reader(voice_id: str | None, remix_config: dict) -> str | None:
    """Reverse-lookup `voice_id` → reader_key (`'narrator'` or a character `key`).

    Voice config lives in `remix_config.voices[]` — the unified collection that
    REPLACED the legacy `narrator` singleton + `characters[].voice_id`. Each
    entry: `{key, voice_id, is_enabled}` where `key` is `'narrator'` or a raw
    character key verbatim (NO `character_` prefix). First-occurrence wins.
    Returns `None` when no match.
    """
    if not voice_id:
        return None
    for v in (remix_config or {}).get("voices") or []:
        if not isinstance(v, dict):
            continue
        if v.get("voice_id") == voice_id:
            key = v.get("key")
            if key:
                return key
    return None


def resolve_voice_for_chunk(chunk: dict, remix_config: dict) -> str | None:
    """Resolve target `voice_id` UUID for a chunk per spec fallback chain.

    Chain: `chunk.reader_key ?? voice_to_reader(chunk.voice_id) ?? 'narrator'`.
    Resolves against `remix_config.voices[]` (key == reader_key, is_enabled),
    returning that entry's `voice_id`; `None` when the reader_key has no enabled
    voices[] entry OR no target voice is set.
    """
    cfg = remix_config or {}
    reader_key = (
        chunk.get("reader_key")
        or voice_to_reader(chunk.get("voice_id"), cfg)
        or "narrator"
    )

    for v in cfg.get("voices") or []:
        if not isinstance(v, dict):
            continue
        if v.get("key") == reader_key and v.get("is_enabled", True):
            return v.get("voice_id") or None

    return None


def needs_regen(chunk: dict, remix_config: dict) -> bool:
    """Predicate: does this chunk require narrate-script regen?

    Two trigger conditions per spec helper `needsRegen`:
      (a) chunk.script_synced is False → text changed in Phase 1.
      (b) resolved voice override differs from chunk.voice_id.
    Both other paths → no regen needed.
    """
    if chunk.get("script_synced") is False:
        return True
    resolved = resolve_voice_for_chunk(chunk, remix_config)
    if resolved is not None and resolved != chunk.get("voice_id"):
        return True
    return False


def iter_enabled_languages(remix_config: dict) -> list[str]:
    """List enabled language codes (`languages[i].code` where `is_enabled=True`).

    Schema parity: `remix_config.languages` is the canonical list; fall back to
    `[narrator.language]` only when missing (legacy remix without languages[]).
    """
    cfg = remix_config or {}
    langs = cfg.get("languages")
    if isinstance(langs, list) and langs:
        out: list[str] = []
        for lang in langs:
            if not isinstance(lang, dict):
                continue
            if lang.get("is_enabled") is False:
                continue
            code = lang.get("code")
            if isinstance(code, str) and code:
                out.append(code)
        if out:
            return out

    fallback = (cfg.get("narrator") or {}).get("language")
    return [fallback] if isinstance(fallback, str) and fallback else []


# ─── Async DB lookups (adapter seam) ───────────────────────────────────────


async def _load_voice_eleven_ids() -> dict[str, str]:
    """One global `SELECT * FROM voices` via the adapter → `{voice_id: eleven_id}`.

    The `voices` table has no book_id (loaded globally in the editor), so the
    adapter's `list_voices` ignores its argument — we pass None and filter
    caller-side, exactly reproducing image-api's `IN (voice_ids)` result set.
    """
    rows = await get_adapter().list_voices(None)  # type: ignore[arg-type]
    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        vid = row.get("id")
        eleven = row.get("eleven_id")
        if vid and eleven:
            out[str(vid)] = eleven
    return out


async def lookup_eleven_id(voice_id: str) -> str | None:
    """Resolve `voices.eleven_id` for one UUID. Returns `None` if missing."""
    try:
        cache = await _load_voice_eleven_ids()
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_lookup_failed voice_id=%s err=%s", voice_id, exc)
        return None
    return cache.get(str(voice_id))


async def build_eleven_id_cache(voice_ids: Iterable[str]) -> dict[str, str]:
    """Return `{voice_id: eleven_id}` for the requested UUIDs.

    Missing rows are simply absent from the dict — caller treats `cache.get(id)`
    returning `None` as "voice deleted between precheck and handler" and pushes
    the per-chunk error in the narrate-script stage.
    """
    ids = sorted({str(v) for v in voice_ids if v})
    if not ids:
        return {}

    try:
        all_ids = await _load_voice_eleven_ids()
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice_cache_build_failed n=%d err=%s", len(ids), exc)
        return {}

    return {vid: all_ids[vid] for vid in ids if vid in all_ids}
