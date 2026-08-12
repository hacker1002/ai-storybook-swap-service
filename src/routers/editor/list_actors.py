"""GET /api/editor/actors?snapshot_id= (spec 10 — casting resolve phía App).

Lazy-loaded (NOT in the book-bundle): the sub-app fetches actor rows to materialize
casting client-side at create-remix time. Two spec decisions encoded here:
  - Unknown snapshot => EMPTY list (200), never 404 — mirrors list_remixes.
  - NO pipeline-completeness filter — full rows (even ones missing rmbgs/upscales)
    so the FE can read batch state and disable the matching presets itself.
"""

from __future__ import annotations

from uuid import UUID

from src.db.adapter import get_adapter


async def list_actors(snapshot_id: UUID) -> dict:
    actors = await get_adapter().list_actors(snapshot_id)
    return {"success": True, "data": {"actors": actors}}
