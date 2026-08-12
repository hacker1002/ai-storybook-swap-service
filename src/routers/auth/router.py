"""Public auth router — prefix /api/editor/auth, NO Bearer dependency.

Deliberately a SEPARATE router from `editor_router`: that one gates every route with
`Depends(require_editor_session)` at the router level, which would 401 the exchange
endpoint forever (exchange is what MINTS the token — there is nothing to Bearer yet).
Same /api/editor prefix but a disjoint path space (/auth/*), so the two never collide.
Registered BEFORE editor_router in main.py for explicitness.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.routers.auth.exchange import exchange

router = APIRouter(prefix="/api/editor/auth")

# 00 §1 — handoff assertion -> access token 12h (public, no Bearer)
router.add_api_route("/exchange", exchange, methods=["POST"])
