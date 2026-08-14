"""Internal-read URL rewrite (ADR-054 — ported from image-api `storage_hosts.to_fetch_url`).

Persisted blob URLs point at the PUBLIC read base (`{STORAGE_PUBLIC_BASE_URL}/files/...`).
When the service re-fetches its own uploads server-side (crop-sheet compose, audio-chunk
combine, upscale source fetch), routing those reads through the public domain wastes an
egress + nginx round-trip on the same box. With `STORAGE_INTERNAL_READ_BASE_URL` set,
`to_fetch_url()` swaps the public prefix for the loopback-nginx base at fetch-time only —
nothing rewritten is ever persisted.

Settings are read at CALL time (never cached module-level) so monkeypatched settings are
honoured in tests — same contract as image-api's module.
"""

from __future__ import annotations

from src.config.settings import settings


def to_fetch_url(url: str) -> str:
    """Rewrite a persisted public URL to the internal loopback-read base, when
    `STORAGE_INTERNAL_READ_BASE_URL` is configured AND `url` starts with the
    public base. No-op otherwise (env empty = the rollback-safe default)."""
    pub = (settings.storage_public_base_url or "").strip().rstrip("/")
    internal = (settings.storage_internal_read_base_url or "").strip().rstrip("/")
    if internal and pub and url.startswith(pub):
        return internal + url[len(pub):]
    return url
