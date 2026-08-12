"""Editor-session JWT verification (spec 00 / auth spec §2.1, §3.3, §5).

Since ADR-053 the service also MINTS (exchange) + REVOKES (denylist). This module
still only VERIFIES; minting lives in `mint_editor_token.py`, the denylist in
`session_stores.py`. Verification stays sync + no external I/O (the denylist is an
in-memory dict lookup). Verify order is MANDATORY (spec 00): missing -> malformed ->
signature/alg -> aud -> exp -> role -> denylist. Getting the order wrong leaks info
(an expired token from another app must read as TOKEN_INVALID, not EXPIRED; a
revoked token must fail role-check FIRST so a viewer still reads as FORBIDDEN).
"""

from __future__ import annotations

from dataclasses import dataclass

import time

import jwt
from fastapi import Header

from src.auth.session_stores import is_revoked
from src.config.settings import settings
from src.core.errors import forbidden, token_expired, token_invalid, token_missing
from src.core.logging import get_logger

logger = get_logger("auth")

_AUDIENCE = "remix-editor"
_ALGORITHMS = ["HS256"]  # hard-coded — reject RS/ES/none (alg-confusion guard)


@dataclass(frozen=True)
class EditorSessionContext:
    admin_ref: str  # claims.admin_ref — audit + rate-limit key (opaque, NOT PII)
    sid: str  # claims.sid — trace session
    consumer: str | None  # claims.consumer (optional)


def verify_editor_session(authorization: str | None) -> EditorSessionContext:
    # 1. header present + Bearer scheme
    if not authorization:
        raise token_missing()
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise token_missing("Authorization header must be 'Bearer <token>'")
    token = parts[1].strip()

    # 2-4. parse + signature(HS256 only) + aud, tried against each configured secret
    # (rotation-ready). `exp` is verified MANUALLY afterwards (verify_exp=False here)
    # so the mandated order signature -> aud -> exp holds: PyJWT internally checks
    # exp before aud, which would mis-report a same-secret wrong-aud expired token as
    # TOKEN_EXPIRED instead of TOKEN_INVALID. `require` still forces claim presence.
    claims: dict | None = None
    for secret in settings.editor_token_secrets:
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=_ALGORITHMS,
                audience=_AUDIENCE,
                options={"require": ["exp", "aud", "role", "admin_ref", "sid"], "verify_exp": False},
            )
            break
        except jwt.InvalidTokenError:
            continue  # bad signature or wrong aud -> try next secret

    if claims is None:
        logger.warning(
            "signature_verify_failed",
            extra={"data": {"configured_secrets": len(settings.editor_token_secrets)}},
        )
        raise token_invalid()

    # 5. exp (manual, so it runs strictly AFTER aud). leeway matches auth spec §2.1.
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or time.time() > exp + settings.editor_token_leeway_seconds:
        raise token_expired()

    # 6. role must be admin (403, distinct from the 401 family above)
    if claims.get("role") != "admin":
        raise forbidden("role must be 'admin'")

    # 7. admin_ref / sid present + non-empty string
    admin_ref = claims.get("admin_ref")
    sid = claims.get("sid")
    if not isinstance(admin_ref, str) or not admin_ref or not isinstance(sid, str) or not sid:
        raise token_invalid("admin_ref and sid required")

    # 8. denylist (ADR-053) — AFTER role so a revoked viewer still reads FORBIDDEN.
    # Revoked collapses to TOKEN_INVALID (NO distinct code — anti-oracle, spec §3.3).
    if is_revoked(sid, admin_ref):
        logger.warning("token_revoked", extra={"data": {"sid": sid, "admin_ref": admin_ref}})
        raise token_invalid()

    consumer = claims.get("consumer")
    return EditorSessionContext(
        admin_ref=admin_ref,
        sid=sid,
        consumer=consumer if isinstance(consumer, str) else None,
    )


async def require_editor_session(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> EditorSessionContext:
    """FastAPI dependency. Registered at ROUTER level so no route is left ungated.
    Reads the header manually (NOT HTTPBearer — its auto_error returns a non-spec
    403 for a missing token)."""
    return verify_editor_session(authorization)
