"""Aggregator router for `/api/remix/*` — ported from image-api (P3b Phase 05).

7 thin transport wrappers over `src/services/remix/*` cores. NO FE consumer today —
these routes are internal/test/debug only (the job pipeline calls the `run_*` cores
in-process; grep of the editor FE shows 0 call-sites). Kept to preserve the ported
manifest + give test-scripts a live surface.

AUTH DELTA vs image-api: image-api gates each route on `X-API-Key`
(`verify_api_key`). Here the whole group gates on the editor-session Bearer dep
(`require_editor_session`) at the ROUTER level — so no remix route can be added
without the gate, and no per-route auth param is needed.

ENVELOPE: remix routes keep image-api's OWN error contract via `RemixDomainError`
(dedicated handler in `error_handler.py`, registered in `main.py`) — NOT the
`/api/editor/*` `ServiceError` envelope.
"""

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.remix import (
    build_crop_sheet,
    detect_crop_geometry,
    detect_mix_defects,
    detect_rmbg_defects,
    detect_swap_defects,
    swap_mix_crop_sheet,
    swap_sprite_sheet,
)

router = APIRouter(
    prefix="/api/remix",
    tags=["remix"],
    dependencies=[Depends(require_editor_session)],
)
router.include_router(build_crop_sheet.router)  # stateless composer
router.include_router(swap_mix_crop_sheet.router)  # generic multi-target mix swap
router.include_router(swap_sprite_sheet.router)  # per-object per-trait sprite-sheet swap
router.include_router(detect_crop_geometry.router)  # 2-step frame detect + classify
router.include_router(detect_swap_defects.router)  # sprite-plane swap defect localization
router.include_router(detect_mix_defects.router)  # mix-plane swap defect localization
router.include_router(detect_rmbg_defects.router)  # rmbg-plane remove-bg defect localization
