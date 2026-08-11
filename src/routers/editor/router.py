"""Editor router group — prefix /api/editor, auth enforced at ROUTER level.

Registering the auth dependency here (not per-route) means no editor route can be
added without a gate. Handlers live in sibling modules; wired via add_api_route so
the aggregator stays the single registration surface (image-api gotcha: empty
collection paths must be registered explicitly).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.editor.create_remix import create_remix
from src.routers.editor.delete_remix import delete_remix
from src.routers.editor.get_book_bundle import get_book_bundle
from src.routers.editor.get_remix import get_remix
from src.routers.editor.list_remixes import list_remixes
from src.routers.editor.update_remix_columns import update_remix_columns

router = APIRouter(prefix="/api/editor", dependencies=[Depends(require_editor_session)])

# 01 — book bundle
router.add_api_route("/book-bundle/{book_id}", get_book_bundle, methods=["GET"])
# 02 — list remixes (query snapshot_id)
router.add_api_route("/remixes", list_remixes, methods=["GET"])
# 03 — get remix
router.add_api_route("/remixes/{remix_id}", get_remix, methods=["GET"])
# 04 — create remix
router.add_api_route("/remixes", create_remix, methods=["POST"], status_code=201)
# 05 — update columns
router.add_api_route("/remixes/{remix_id}/columns", update_remix_columns, methods=["PATCH"])
# 06 — delete remix
router.add_api_route("/remixes/{remix_id}", delete_remix, methods=["DELETE"])
