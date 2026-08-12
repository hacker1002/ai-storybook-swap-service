"""GET /api/editor/book-bundle/{book_id} (spec 01).

The sub-app's single bootstrap read: book + FULL current snapshot + humans + voices.
EDITOR-GRADE — NO layer filtering (unlike player get-book-preview; copying that
would drop data the editor needs). Snapshot missing => 404 (data integrity: a broken
clone), never 200-with-null.

`artStyle` is retained in the contract (additive-only) but is ALWAYS null: App DB
rev 2 does not clone `art_styles` and drops `books.artstyle_id`
(spec 01 + app-db-clone-tables §Hệ quả swap-service, 260812).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from src.core.errors import not_found
from src.db.adapter import get_adapter

_CONTRACT_VERSION = 1


async def get_book_bundle(book_id: UUID) -> dict:
    adapter = get_adapter()

    book = await adapter.get_book(book_id)
    if book is None:
        raise not_found("Book not found")

    snapshot = await adapter.get_current_snapshot(book_id, book.get("current_version"))
    if snapshot is None:
        raise not_found("Current snapshot not found")

    # Independent reads in parallel once book is known.
    humans, voices = await asyncio.gather(
        adapter.list_humans(book_id),
        adapter.list_voices(book_id),
    )

    return {
        "success": True,
        "data": {
            "contractVersion": _CONTRACT_VERSION,
            "book": book,
            "snapshot": snapshot,
            # Contract-parity constant; App DB rev 2 clones no art_styles (see module docstring).
            "artStyle": None,
            "humans": humans,
            "voices": voices,
        },
    }
