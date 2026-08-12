"""S2S guard for /internal/* — X-API-Key, fail-closed (ADR-053).

Registered at ROUTER level so no /internal route is left ungated (same principle as
`require_editor_session`). If `INTERNAL_API_KEY` is unset the guard rejects EVERY
request (fail-closed): the service still boots (Admin App P2 may not exist yet) but
revoke is effectively disabled — a one-time boot warning is emitted from `main.py`.
Compares with `secrets.compare_digest` (constant-time — no timing oracle on the key).
"""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Header

from src.config.settings import settings
from src.core.errors import api_key_invalid


async def require_internal_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    configured = settings.internal_api_key
    if not configured or x_api_key is None:
        raise api_key_invalid()  # fail-closed: no key configured OR none supplied
    if not compare_digest(x_api_key.encode(), configured.encode()):
        raise api_key_invalid()
