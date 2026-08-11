"""Audit stamping for mutations (spec 00 §Audit — MANDATORY, not optional).

Role-wide admin sessions carry no per-user ownership, so `admin_ref` in the audit
trail is the ONLY attribution. Every create/update/delete emits a structured log
line. Never log JSONB content (book text / URLs) — only column names + sizes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.auth.editor_session import EditorSessionContext
from src.core.logging import get_logger

logger = get_logger("audit")


def audit(
    ctx: EditorSessionContext,
    endpoint: str,
    resource_id: str | None = None,
    **extra: object,
) -> None:
    logger.info(
        "mutation",
        extra={
            "data": {
                "event": "mutation",
                "endpoint": endpoint,
                "admin_ref": ctx.admin_ref,
                "sid": ctx.sid,
                "resource_id": resource_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                **extra,
            }
        },
    )
