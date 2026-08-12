"""Internal S2S router — prefix /internal, X-API-Key at ROUTER level.

Never accepts an editor-session Bearer in place of the S2S key (and vice-versa). No
route here is left ungated (same principle as editor_router). Access-logged (NOT in
main._SKIP_LOG_PATHS) so revokes are auditable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.internal_api_key import require_internal_api_key
from src.routers.internal.revoke import revoke

router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_api_key)])

# 00 §3 — revoke a session (sid) or all sessions of an admin (admin_ref)
router.add_api_route("/auth/revoke", revoke, methods=["POST"])
