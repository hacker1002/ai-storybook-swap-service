"""POST /internal/auth/revoke (spec 00 §3) — S2S, adds a session/admin to the denylist.

Called by the Admin App backend when it removes an admin role / locks an account.
Idempotent. Guard (X-API-Key) is enforced at ROUTER level. In-memory denylist is
lost on restart (ADR-053 trade-off) — App may re-push to be certain; the endpoint
is safe to call repeatedly.
"""

from __future__ import annotations

from src.auth.session_stores import revoke as revoke_session
from src.core.logging import get_logger
from src.models.auth import RevokeRequest

logger = get_logger("auth")


async def revoke(body: RevokeRequest) -> dict:
    revoke_session(sid=body.sid, admin_ref=body.admin_ref)
    logger.info("auth_revoke", extra={"data": {"sid": body.sid, "admin_ref": body.admin_ref}})
    return {"success": True}
