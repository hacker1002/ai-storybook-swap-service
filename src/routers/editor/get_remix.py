"""GET /api/editor/remixes/{remix_id} (spec 03). Not found => 404 (FE maps to null)."""

from __future__ import annotations

from uuid import UUID

from src.core.errors import not_found
from src.db.adapter import get_adapter


async def get_remix(remix_id: UUID) -> dict:
    remix = await get_adapter().get_remix(remix_id)
    if remix is None:
        raise not_found("Remix not found")
    return {"success": True, "data": {"remix": remix}}
