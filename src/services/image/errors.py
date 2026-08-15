"""Domain errors for image core services.

Ported VERBATIM from `ai-storybook-python-api/src/services/image/errors.py` (P3b).

Raised by `upscale_core.run_upscale()` instead of HTTPException so callers — the
in-process job handler (ADR-031) — decide how to surface the failure. In this
service `run_upscale` is only reached from the `remix_upscale` job handler, which
catches it as a graceful per-crop fallback (there is NO HTTP route mounting the
upscale core here), so `ImageDomainError` never needs an app-level handler.

Parity shape with `remix.errors.RemixDomainError`.
"""

from __future__ import annotations

from typing import Any, Optional


class ImageDomainError(Exception):
    """Stable failure shape for image services.

    `status` mirrors the HTTP status the router/handler would emit.
    `code` is the spec-defined error code (e.g. `SSRF_BLOCKED`,
    `INVALID_IMAGE_DATA`, `REPLICATE_ERROR`, `OUTPUT_FETCH_ERROR`).
    `details` is an optional bag of structured context (PII-safe only).
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
