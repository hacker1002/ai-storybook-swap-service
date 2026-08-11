"""DELETE /api/editor/remixes/{remix_id} (spec 06).

Idempotent: rowcount 0 -> 200 {deleted:false} (NOT 404). Guard: any active
(queued|running) job for this remix -> 409 REMIX_BUSY (stronger than editor today,
accepted divergence per spec 06).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.errors import remix_busy
from src.db.adapter import get_adapter


async def delete_remix(
    remix_id: UUID,
    ctx: EditorSessionContext = Depends(require_editor_session),
) -> dict:
    adapter = get_adapter()
    if await adapter.has_active_job(remix_id):
        raise remix_busy("Cannot delete remix with an active job")

    deleted = await adapter.delete_remix(remix_id)
    audit(ctx, "DELETE /api/editor/remixes/{id}", str(remix_id), deleted=deleted)
    return {"success": True, "data": {"remix_id": str(remix_id), "deleted": deleted}}
