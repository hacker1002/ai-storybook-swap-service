"""Aggregator router for `/api/image/*` — ported from image-api (P3c).

ONLY `upscale-image` is ported (the remix sub-app's upscale tab). This endpoint
lives in image-api's `image` DOMAIN, NOT `retouch` — it was MISSING from manifest 08
(a documented gap, filled here). AUTH DELTA: editor-session Bearer at the ROUTER
level, no per-route X-API-Key.

ENVELOPE: keeps image-api's OWN contract via `ImageDomainError` (dedicated app-level
handler in `main.py`) — NOT the `/api/editor/*` `ServiceError` envelope.
"""

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.image import upscale_image

router = APIRouter(
    prefix="/api/image",
    tags=["image"],
    dependencies=[Depends(require_editor_session)],
)
router.include_router(upscale_image.router)
