"""Shared router helpers used by the ported `/api/remix/*` handlers.

`error_response` + `url_host` are ported VERBATIM from image-api's
`src/routers/_shared/deps.py` so the ported remix routers stay byte-identical. The
`verify_api_key` dep is deliberately OMITTED — remix routes here gate on the
editor-session Bearer dep (`require_editor_session`) at the router group level.

`error_response` returns an `HTTPException` whose `detail` carries the image-api
error envelope `{success, error:{code,message,details?}}` — kept identical to
image-api so a ported handler's last-resort 500 path matches. The primary remix
error path is `RemixDomainError` (its dedicated handler renders the flat envelope).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

__all__ = ["error_response", "url_host"]


def error_response(
    status: int,
    code: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(
        status_code=status,
        detail={"success": False, "error": error},
    )


def url_host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"
