"""POST /api/editor/remixes (spec 04).

The service does NOT re-implement clone/text-swap/crop-sheet logic — it validates
shape-light, normalizes, stamps audit, writes DB. Normalization (spec 04):
  name empty -> 'New Remix'; props/mixes/sprites absent -> []; distribution absent
  -> NULL; rmbgs/upscales ALWAYS [] (client value ignored); owner_id NULL.
Precondition: snapshot must exist -> else 422 SNAPSHOT_NOT_FOUND (NOT 404).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.errors import snapshot_not_found, validation_error
from src.db.adapter import get_adapter
from src.models.editor.remixes import CreateRemixPayload

_DEFAULT_NAME = "New Remix"


async def create_remix(
    payload: CreateRemixPayload,
    ctx: EditorSessionContext = Depends(require_editor_session),
) -> dict:
    try:
        snapshot_id = UUID(payload.snapshot_id)
    except (ValueError, AttributeError, TypeError):
        raise validation_error("snapshot_id must be a UUID")

    adapter = get_adapter()
    if not await adapter.snapshot_exists(snapshot_id):
        raise snapshot_not_found(f"Snapshot {snapshot_id} does not exist")

    name = payload.name.strip() if isinstance(payload.name, str) else ""
    row: dict = {
        "snapshot_id": snapshot_id,
        "name": name or _DEFAULT_NAME,
        "remix_config": payload.remix_config,
        "illustration": payload.illustration,
        "characters": payload.characters,
        "props": payload.props if payload.props is not None else [],
        "mixes": payload.mixes if payload.mixes is not None else [],
        "sprites": payload.sprites if payload.sprites is not None else [],
        "rmbgs": [],  # job-only — never client-supplied
        "upscales": [],
        "owner_id": None,  # App DB has no user directory
    }
    if payload.distribution is not None:
        row["distribution"] = payload.distribution

    remix = await adapter.insert_remix(row)
    audit(ctx, "POST /api/editor/remixes", str(remix["id"]), snapshot_id=str(snapshot_id))
    return {"success": True, "data": {"remix": remix}}
