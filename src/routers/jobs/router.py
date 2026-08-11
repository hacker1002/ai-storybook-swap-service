"""Jobs router group — prefix /api/jobs, auth at ROUTER level.

P3b will add enqueue/cancel routes HERE. `/status` is registered so it can never
be shadowed by a future `/{job_id}` route (register static before dynamic).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.jobs.get_job_status import get_job_status

router = APIRouter(prefix="/api/jobs", dependencies=[Depends(require_editor_session)])

# 07 — batch status polling. MUST stay ahead of any /{job_id} route P3b adds.
router.add_api_route("/status", get_job_status, methods=["GET"])
