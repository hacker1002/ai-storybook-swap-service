"""Mint editor-session access tokens (ADR-053 — the service now OWNS minting).

Called by the exchange handler AFTER a handoff assertion is verified + its jti
consumed. Claims match the verify contract in `editor_session.py` (aud=remix-editor,
role=admin, admin_ref, sid, iat, exp). The `sid` is the assertion's jti — a stable
session id that survives into the audit trail and is the revoke key.

Signs with the LAST configured secret (`editor_token_secrets[-1]` = newest during a
rotation) while verify accepts ANY configured secret — so rotating in a new secret
never invalidates tokens minted under the old one. NOT reused from the dev signer in
`scripts/` (that lives outside `src/` on purpose and is test-harness only).
"""

from __future__ import annotations

import time

import jwt

from src.config.settings import settings

_AUDIENCE = "remix-editor"


def mint_access_token(admin_ref: str, sid: str, consumer: str | None = None) -> tuple[str, int]:
    """Return (access_token, expires_in_seconds). Flat 12h, HS256."""
    now = int(time.time())
    ttl = settings.editor_access_token_ttl_seconds
    claims: dict = {
        "aud": _AUDIENCE,
        "role": "admin",
        "admin_ref": admin_ref,
        "sid": sid,
        "iat": now,
        "exp": now + ttl,
    }
    if consumer:
        claims["consumer"] = consumer
    token = jwt.encode(claims, settings.editor_token_secrets[-1], algorithm="HS256")
    return token, ttl
