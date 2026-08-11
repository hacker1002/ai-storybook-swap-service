"""Jobs router group — prefix /api/jobs, auth at ROUTER level.

Route-order invariant: STATIC routes (`/status`, every `/remix/...` enqueue) MUST
be registered BEFORE the DYNAMIC `/{job_id}/cancel` route, otherwise `cancel`'s
`{job_id}` pattern would shadow them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.jobs import (
    cancel_job,
    enqueue_remix_audio_swap,
    enqueue_remix_detect_defects,
    enqueue_remix_detect_mix_defects,
    enqueue_remix_detect_rmbg_defects,
    enqueue_remix_mix_swap,
    enqueue_remix_rmbg,
    enqueue_remix_sprite_swap,
    enqueue_remix_upscale,
)
from src.routers.jobs.get_job_status import get_job_status

router = APIRouter(prefix="/api/jobs", dependencies=[Depends(require_editor_session)])

# 07 — batch status polling. Static — MUST stay ahead of any /{job_id} route.
router.add_api_route("/status", get_job_status, methods=["GET"])

# P3b enqueue routes — all static `/remix/{remix_id}/...` (register before cancel).
router.include_router(enqueue_remix_sprite_swap.router)       # POST /remix/{remix_id}/sprite-swap
router.include_router(enqueue_remix_mix_swap.router)          # POST /remix/{remix_id}/mix-swap
router.include_router(enqueue_remix_audio_swap.router)        # POST /remix/{remix_id}/audio-swap
router.include_router(enqueue_remix_rmbg.router)              # POST /remix/{remix_id}/rmbg
router.include_router(enqueue_remix_upscale.router)           # POST /remix/{remix_id}/upscale
router.include_router(enqueue_remix_detect_defects.router)    # POST /remix/{remix_id}/detect-sprite-defects
router.include_router(enqueue_remix_detect_mix_defects.router)   # POST /remix/{remix_id}/detect-mix-defects
router.include_router(enqueue_remix_detect_rmbg_defects.router)  # POST /remix/{remix_id}/detect-rmbg-defects

# DYNAMIC — MUST be registered LAST so `/{job_id}` does not shadow the static routes.
router.include_router(cancel_job.router)                     # POST /{job_id}/cancel
