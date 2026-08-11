"""Aggregator router for `/api/retouch/*` — ported from image-api (P3c).

ONLY the 2 endpoints the remix sub-app's EditImageModal actually calls (Phase 00
call-site verification): edit-object-image (inpaint) + image-remove-bg. The other
9 retouch endpoints image-api hosts (outpaint, remove-text, layering, segment,
crop-object, generate-background, detect-objects/texts, generate-narration) are
DELIBERATELY out of scope — no remix surface calls them (YAGNI; additive later).

AUTH DELTA vs image-api: the whole group gates on the editor-session Bearer dep
(`require_editor_session`) at the ROUTER level — no per-route X-API-Key.

ENVELOPE: these ported routes keep image-api's OWN error contract (`error_response`
HTTPException → `{detail:{success,error}}`; `RemixDomainError` for UNSUPPORTED_MODEL
via its dedicated handler) — NOT the `/api/editor/*` `ServiceError` envelope.
"""

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.retouch import edit_object_image, image_remove_bg

router = APIRouter(
    prefix="/api/retouch",
    tags=["retouch"],
    dependencies=[Depends(require_editor_session)],
)
router.include_router(edit_object_image.router)  # Gemini image-edit (inpaint)
router.include_router(image_remove_bg.router)  # Bria/851-labs remove-background
