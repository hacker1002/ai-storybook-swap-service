"""Shared OPTIONAL-UUID attribution field types for request models.

Cross-cutting attribution inputs (`snapshotId` / `remixId` / `bookId`) threaded
from a sync request body into an `AiCallContext` (`src.services.ai_usage`). Each is
an OPTIONAL UUID: a `None` value passes untouched; a non-empty string MUST parse as
a UUID, else a Pydantic `ValueError` → the global handler returns 400
`VALIDATION_ERROR` (body error, memory `reference_image_api_validation_http_codes`).

Attribution-only: these NEVER read/authorize data — they only tag the AI-usage log
row so cost rolls up to a book (`snapshotId` → the logger resolves `book_id` at
write time), straight to a book (`bookId`), or to a remix (`remixId`, the billing
DISCRIMINATOR — remix cost is tracked separately from the parent book).

Illustration + sketch keep their OWN local optional-UUID validators (pre-existing,
kept to avoid churn); every OTHER request model (retouch / text / upscale / remix)
uses these shared types (DRY).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Callable

from pydantic import AfterValidator

__all__ = ["SnapshotId", "RemixId", "BookId"]


def _optional_uuid(field_name: str) -> Callable[[str], str]:
    """Build an `AfterValidator` for an OPTIONAL UUID attribution field.

    Runs only on the `str` branch of a `<Type> | None` union — a `None` value never
    reaches it (the union resolves `None` first). A non-empty string must parse as a
    UUID; a blank or malformed value raises `ValueError` → 400 `VALIDATION_ERROR`
    (global handler). Returns the trimmed value.
    """

    def _validate(v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError(f"{field_name} must be a non-empty string")
        try:
            uuid.UUID(s)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid UUID") from exc
        return s

    return _validate


SnapshotId = Annotated[str, AfterValidator(_optional_uuid("snapshotId"))]
RemixId = Annotated[str, AfterValidator(_optional_uuid("remixId"))]
BookId = Annotated[str, AfterValidator(_optional_uuid("bookId"))]
