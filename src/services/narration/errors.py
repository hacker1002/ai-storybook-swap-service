"""Domain errors for narration core services.

Raised by `narrate_script_core` / `combine_audio_chunks_core` instead of
HTTPException, so callers (router OR in-process job handler) can decide how
to surface the failure. Router maps to FastAPI HTTPException; job handler
records into `step_details.errors[]`.
"""

from __future__ import annotations

from typing import Any, Optional


class NarrationDomainError(Exception):
    """Stable failure shape for narration services.

    `status` mirrors the HTTP status the router will emit (also used as a
    severity hint by the job handler).
    `code` is the spec-defined error code (e.g. `ELEVEN_RATE_LIMITED`).
    `details` is an optional bag of structured context (e.g. `failedChunkIndex`).
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
