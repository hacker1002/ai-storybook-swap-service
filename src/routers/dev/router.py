"""Dev router group — prefix /api/dev, tooling OUTSIDE the editor-facing surface.

NO `require_editor_session` dependency here on purpose: these routes ISSUE tokens,
they don't consume them — each route carries its own gate (dev key header). The
whole group is only registered when `DEV_MINT_ENABLED=true` (main.py); a default
deploy has no /api/dev/* paths at all.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.routers.dev.mint_editor_token import mint_editor_token

router = APIRouter(prefix="/api/dev")

# 10 — DEV-only mint (stand-in for Admin App backend)
router.add_api_route("/mint-editor-token", mint_editor_token, methods=["POST"])
