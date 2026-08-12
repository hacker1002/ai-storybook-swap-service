"""PATCH /api/editor/remixes/{remix_id}/columns (spec 05).

Whole-column, last-writer-wins, no lock (ADR-052). Any key outside the 9-column
writable allowlist — including create-only `remix_config` or `id`/`snapshot_id` —
REJECTS THE WHOLE REQUEST with 400 COLUMN_NOT_WRITABLE (never silent-drop; FE
would think it persisted). `rmbgs`/`upscales` ARE writable: the FE remix-store
owns batch lifecycle (add/remove/import/relayout/takeFinalBack) client-side for
all 3 stage columns (see core/remix_columns.py). rowcount 0 -> 404.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.errors import column_not_writable, not_found
from src.core.remix_columns import WRITABLE_REMIX_COLUMNS
from src.db.adapter import get_adapter
from src.models.editor.remixes import UpdateRemixColumnsPayload


async def update_remix_columns(
    remix_id: UUID,
    payload: UpdateRemixColumnsPayload,
    ctx: EditorSessionContext = Depends(require_editor_session),
) -> dict:
    columns = payload.columns
    for key in columns:
        if key not in WRITABLE_REMIX_COLUMNS:
            raise column_not_writable(key)

    updated = await get_adapter().update_remix_columns(remix_id, columns)
    if not updated:
        raise not_found("Remix not found")

    updated_columns = sorted(columns)
    audit(ctx, "PATCH /api/editor/remixes/{id}/columns", str(remix_id), updated_columns=updated_columns)
    return {"success": True, "data": {"remix_id": str(remix_id), "updated_columns": updated_columns}}
