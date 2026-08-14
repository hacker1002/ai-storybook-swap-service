"""AppStorageAdapter Protocol + module-global accessor.

The single Storage surface of the service. Every module imports the
`get_storage` SYMBOL (not the concrete class) so tests swap one seam via
`set_storage(FakeAppStorageAdapter())` — mirrors `src/db/adapter.py` 1:1 (one
accessor, not per-module get-client).

Boundary: NO Supabase SDK. Two concrete impls, chosen at wiring by env presence
(`storage/factory.build_storage_adapter`, ADR-054): `StorageServiceRestStorage`
(httpx S2S → self-hosted storage service `:8200`) or, as the rollback path,
`supabase_rest.SupabaseRestStorage` (Supabase Storage REST over httpx).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AppStorageAdapter(Protocol):
    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str,
        bucket: str | None = None,
        upsert: bool = True,
    ) -> str:
        """Upload bytes → return the object's public URL."""
        ...

    def public_url(self, path: str, bucket: str | None = None) -> str:
        """Deterministic public URL (no I/O)."""
        ...

    async def create_signed_url(
        self, path: str, expires_in: int, bucket: str | None = None
    ) -> str:
        """Mint a short-lived signed URL for a private object."""
        ...

    async def delete(self, path: str, bucket: str | None = None) -> None:
        """Best-effort DELETE of a storage object."""
        ...


_STORAGE: AppStorageAdapter | None = None


def set_storage(storage: AppStorageAdapter) -> None:
    global _STORAGE
    _STORAGE = storage


def get_storage() -> AppStorageAdapter:
    if _STORAGE is None:
        raise RuntimeError(
            "AppStorageAdapter not set — wire it in lifespan startup (or a test fixture)"
        )
    return _STORAGE
