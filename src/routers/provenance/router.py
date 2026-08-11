"""Aggregator router for `/api/provenance/*` (P3c Gap 2).

AUTH: editor-session Bearer at the ROUTER level (image-api uses Supabase user JWT).
The editor session is role-wide admin, so authz is an existence check only — no
per-book gate (see `get_ai_request_references` docstring).

ENVELOPE: read handler returns a plain `{success, data, meta}` dict (image-api
parity); a 404 goes through `error_response` (HTTPException → `{detail:{success,error}}`).
"""

from fastapi import APIRouter, Depends

from src.auth.editor_session import require_editor_session
from src.routers.provenance import get_ai_request_references

router = APIRouter(
    prefix="/api/provenance",
    tags=["provenance"],
    dependencies=[Depends(require_editor_session)],
)
router.include_router(get_ai_request_references.router)
