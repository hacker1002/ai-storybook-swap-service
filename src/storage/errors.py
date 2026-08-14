"""Neutral storage exception shared by BOTH adapter impls (ADR-054).

`StorageUploadError` was born in `supabase_rest.py`; the storage-service cutover
adds a SECOND impl (`storage_service_rest.py`), so the exception moves here to a
backend-agnostic home. `supabase_rest` re-exports it → every `except
StorageUploadError` at the call sites / cores keeps its EXACT import path AND
catches the same class regardless of which backend raised it (one definition, no
`is`-identity drift between the two adapters).

`status_code` is an ADDITIVE optional field: the storage-service impl carries the
HTTP status (409/413/415/507/5xx) for diagnostics; the Supabase impl leaves it
`None`. Handler maps the error to 500.
"""

from __future__ import annotations


class StorageUploadError(Exception):
    """Raised when a Storage write (upload/sign) fails. Handler maps to 500.

    `status_code` is the upstream HTTP status when known (storage-service impl);
    `None` for transport-exhausted / Supabase-legacy failures.
    """

    def __init__(
        self,
        path: str,
        bucket: str,
        reason: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"Storage op failed bucket={bucket} path={path}: {reason}")
        self.path = path
        self.bucket = bucket
        self.reason = reason
        self.status_code = status_code
