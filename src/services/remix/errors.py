"""Domain errors for remix core services.

Raised by `crop_sheet_composer.compose_crop_sheet()` instead of HTTPException
so callers (router OR in-process job handler) decide how to surface the
failure. Router maps to FastAPI HTTPException via `error_response()`.

Parity shape with `narration.errors.NarrationDomainError`.
"""

from __future__ import annotations

from typing import Any, Optional


class RemixDomainError(Exception):
    """Stable failure shape for remix services.

    `status` mirrors the HTTP status the router will emit.
    `code` is the spec-defined error code (e.g. `EMPTY_CROPS`,
    `GEOMETRY_OUT_OF_BOUNDS`, `ALL_CROPS_FAILED`).
    `details` is an optional bag of structured context.
    """

    __slots__ = ("status", "code", "message", "details")

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}
