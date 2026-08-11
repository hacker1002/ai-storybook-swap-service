"""POST /api/jobs/{job_id}/cancel — set `cancel_requested=true` flag.

Ported from image-api with the P3b deltas:
  - `sb.table(...)` → `get_adapter()`.
  - OWNERSHIP DELTA: NO per-user owner check (role-wide editor session). Existence
    check only (`get_job` → 404 JOB_NOT_FOUND). Any authorized caller may cancel
    any job. Bearer is enforced at the router-group level.
  - A job already in a terminal status → 200 no-op (NOT an error).

⚠️ ROUTE ORDER: `/{job_id}/cancel` is a DYNAMIC route — it MUST be registered
AFTER the static `/status` (P3a) and `/remix/...` routes so it can never shadow
them. Wiring is central (`router.py`).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.db.adapter import get_adapter
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)

router = APIRouter()

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    session: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()

    try:
        row = await adapter.get_job(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cancel_job_lookup_failed id=%s", job_id)
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR", message="lookup failed"
        ) from exc

    if not row:
        raise RemixDomainError(
            status=404, code="JOB_NOT_FOUND", message=f"job {job_id} not found"
        )

    current_status = row["status"]

    if current_status in TERMINAL_STATUSES:
        return {
            "success": True,
            "data": {
                "job_id": job_id,
                "cancel_requested": False,
                "current_status": current_status,
                "note": "job_already_terminal",
            },
        }

    try:
        await adapter.update_job(job_id, {"cancel_requested": True})
    except Exception as exc:  # noqa: BLE001
        logger.exception("cancel_job_update_failed id=%s", job_id)
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR", message="update failed"
        ) from exc

    audit(session, endpoint="jobs.cancel", resource_id=job_id, prev_status=current_status)
    logger.info("job_cancel_requested id=%s prev_status=%s", job_id, current_status)
    return {
        "success": True,
        "data": {
            "job_id": job_id,
            "cancel_requested": True,
            "current_status": current_status,
            "note": "handler will exit at next checkpoint",
        },
    }
