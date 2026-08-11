"""AppDbAdapter Protocol + module-global accessor.

The single DB surface of the service (~17 methods). Every module imports the
`get_adapter` SYMBOL (not the concrete class) so tests swap one seam via
`set_adapter(FakeAppDbAdapter())` — mirrors image-api's per-module patch trap, but
simpler (one accessor, not per-module `get_supabase_client`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class AppDbAdapter(Protocol):
    # --- reads for book-bundle (spec 01) ---
    async def get_book(self, book_id: UUID) -> dict | None: ...
    async def get_current_snapshot(self, book_id: UUID, current_version: UUID | None) -> dict | None: ...
    async def get_art_style(self, art_style_id: UUID) -> dict | None: ...
    async def list_humans(self, book_id: UUID) -> list[dict]: ...
    async def list_voices(self, book_id: UUID) -> list[dict]: ...

    # --- remixes CRUD (specs 02-06) ---
    async def list_remixes(self, snapshot_id: UUID) -> list[dict]: ...
    async def get_remix(self, remix_id: UUID) -> dict | None: ...
    async def snapshot_exists(self, snapshot_id: UUID) -> bool: ...
    async def insert_remix(self, row: dict) -> dict: ...
    async def update_remix_columns(self, remix_id: UUID, columns: dict) -> bool: ...  # False = rowcount 0
    async def delete_remix(self, remix_id: UUID) -> bool: ...  # False = did not exist

    # --- jobs (spec 07 + P3b) ---
    async def insert_job(self, row: dict) -> dict: ...
    async def get_jobs(self, ids: list[UUID]) -> list[dict]: ...
    async def update_job(self, job_id: UUID, fields: dict, expect_status: str | None = None) -> bool: ...
    async def find_active_job(self, remix_id: UUID, job_type: str) -> dict | None: ...
    async def has_active_job(self, remix_id: UUID) -> bool: ...

    # --- AI logging (P3b) ---
    async def insert_ai_log(self, row: dict) -> None: ...

    # --- bridge (remixes has no book_id) ---
    async def get_book_id_for_remix(self, remix_id: UUID) -> UUID | None: ...


_ADAPTER: AppDbAdapter | None = None


def set_adapter(adapter: AppDbAdapter) -> None:
    global _ADAPTER
    _ADAPTER = adapter


def get_adapter() -> AppDbAdapter:
    if _ADAPTER is None:
        raise RuntimeError("AppDbAdapter not set — wire it in lifespan startup (or a test fixture)")
    return _ADAPTER
