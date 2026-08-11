"""GET /api/editor/remixes?snapshot_id= (spec 02).

Mirrors the editor select: a non-existent snapshot returns an EMPTY list (200),
never 404. Ordering created_at DESC.
"""

from __future__ import annotations

from uuid import UUID

from src.db.adapter import get_adapter


async def list_remixes(snapshot_id: UUID) -> dict:
    remixes = await get_adapter().list_remixes(snapshot_id)
    return {"success": True, "data": {"remixes": remixes}}
