"""POST /api/editor/auth/exchange (spec 00 §1) — handoff assertion -> access token.

The ONLY editor-facing endpoint with NO Bearer. Because it is public it MUST have:
per-IP rate limit, one-time jti, a hard 60s assertion TTL clamp, and error
non-differentiation (every failure -> 401 HANDOFF_INVALID, no oracle). Response body
is FLAT { access_token, expires_in, admin_name? } — the single divergence from the
{success,data} envelope (spec + FE both specify flat; recorded in CHANGELOG).
"""

from __future__ import annotations

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from src.auth.mint_editor_token import mint_access_token
from src.auth.session_stores import mark_jti_used
from src.config.settings import settings
from src.core.errors import handoff_invalid, rate_limited
from src.core.logging import get_logger
from src.models.auth import ExchangeRequest
from src.services.ip_rate_limit import check_ip

logger = get_logger("auth")

_HANDOFF_AUD = "remix-editor-handoff"
_ALGORITHMS = ["HS256"]  # hard-coded — reject RS/ES/none (alg-confusion guard)
# Hard TTL clamp on the assertion. Stricter than a plain exp check: an Admin App bug
# that signs a long-lived assertion is rejected (defense-in-depth). Small margin
# absorbs clock skew between App and this service. Written into the App contract.
_MAX_ASSERTION_TTL = 60
_CLAMP_MARGIN = 5


def _sanitize_admin_name(value: object) -> str | None:
    """Clamp <=100 chars + strip control chars before echoing an App-supplied name."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable())[:100].strip()
    return cleaned or None


async def exchange(request: Request, body: ExchangeRequest) -> JSONResponse:
    client_ip = request.client.host if request.client else "unknown"

    # 1. rate limit (public endpoint)
    if not check_ip(client_ip, settings.auth_exchange_rate_limit_per_min):
        logger.warning("auth_exchange_rate_limited", extra={"data": {"ip": client_ip}})
        raise rate_limited()

    # 2. verify assertion against each configured handoff secret (rotation-ready).
    # PyJWT verifies signature + aud + exp; `require` forces claim presence. Any
    # failure -> HANDOFF_INVALID (no differentiation).
    claims: dict | None = None
    for secret in settings.editor_handoff_secrets:
        try:
            claims = jwt.decode(
                body.code,
                secret,
                algorithms=_ALGORITHMS,
                audience=_HANDOFF_AUD,
                options={"require": ["exp", "iat", "jti", "admin_ref"]},
            )
            break
        except jwt.InvalidTokenError:
            continue
    if claims is None:
        raise handoff_invalid()

    # 3. hard TTL clamp (MANDATORY — App contract) — exp - iat must be <= 60s (+margin)
    iat = claims.get("iat")
    exp = claims.get("exp")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise handoff_invalid()
    if exp - iat > _MAX_ASSERTION_TTL + _CLAMP_MARGIN:
        logger.warning("auth_exchange_ttl_clamp", extra={"data": {"ip": client_ip}})
        raise handoff_invalid()

    admin_ref = claims.get("admin_ref")
    jti = claims.get("jti")
    if not isinstance(admin_ref, str) or not admin_ref or not isinstance(jti, str) or not jti:
        raise handoff_invalid()

    # 4. one-time — consume the jti (replay -> HANDOFF_INVALID)
    if not mark_jti_used(jti):
        logger.warning("auth_exchange_replay", extra={"data": {"admin_ref": admin_ref, "ip": client_ip}})
        raise handoff_invalid()

    # 5. mint access token — sid = jti (stable session id + revoke key)
    consumer = claims.get("consumer") if isinstance(claims.get("consumer"), str) else None
    access_token, expires_in = mint_access_token(admin_ref, sid=jti, consumer=consumer)
    admin_name = _sanitize_admin_name(claims.get("admin_name"))

    # 6. audit (never log the code / token)
    logger.info(
        "auth_exchange",
        extra={"data": {"admin_ref": admin_ref, "sid": jti, "consumer": consumer, "ip": client_ip}},
    )

    payload: dict = {"access_token": access_token, "expires_in": expires_in}
    if admin_name:
        payload["admin_name"] = admin_name
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})
