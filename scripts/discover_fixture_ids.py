#!/usr/bin/env python3
"""DEV ONLY — discover real book/snapshot/remix ids from the local App DB and write
them to test-scripts/fixtures/local-ids.env (gitignored). No `psql` on this machine
so we query via asyncpg.

Usage: uv run --with asyncpg python scripts/discover_fixture_ids.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import asyncpg

_DSN = os.environ.get("APP_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
_OUT = pathlib.Path(__file__).resolve().parent.parent / "test-scripts" / "fixtures" / "local-ids.env"


async def main() -> None:
    conn = await asyncpg.connect(_DSN)
    book = await conn.fetchrow(
        "SELECT id, current_version FROM books WHERE current_version IS NOT NULL LIMIT 1"
    )
    if book is None:
        print("No book with current_version found — cannot build fixtures.")
        await conn.close()
        return
    book_id = book["id"]
    snapshot_id = book["current_version"]
    remix = await conn.fetchrow("SELECT id FROM remixes WHERE snapshot_id = $1 LIMIT 1", snapshot_id)
    remix_id = remix["id"] if remix else ""
    job = await conn.fetchrow("SELECT id FROM background_jobs LIMIT 1")
    job_id = job["id"] if job else ""
    # spec 10 — a snapshot that actually HAS actors rows (may differ from the primary
    # SNAPSHOT_ID). Empty when the table is bare; test-list-actors.sh falls back to
    # SNAPSHOT_ID and asserts only the empty-branch. Table `actors` is owned by the
    # ops clone process — do NOT seed/DDL it here.
    actors = await conn.fetchrow("SELECT snapshot_id FROM actors ORDER BY created_at ASC LIMIT 1")
    actors_snapshot_id = actors["snapshot_id"] if actors else ""
    await conn.close()

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(
        "# DEV fixture ids (gitignored) — regenerate with discover_fixture_ids.py\n"
        f"BOOK_ID={book_id}\n"
        f"SNAPSHOT_ID={snapshot_id}\n"
        f"REMIX_ID={remix_id}\n"
        f"JOB_ID={job_id}\n"
        f"ACTORS_SNAPSHOT_ID={actors_snapshot_id}\n"
    )
    print(f"Wrote {_OUT}")
    print(f"  BOOK_ID={book_id}")
    print(f"  SNAPSHOT_ID={snapshot_id}")
    print(f"  REMIX_ID={remix_id or '(none — create via test-create-remix.sh)'}")
    print(f"  JOB_ID={job_id or '(none)'}")
    print(f"  ACTORS_SNAPSHOT_ID={actors_snapshot_id or '(none — actors table empty)'}")


if __name__ == "__main__":
    asyncio.run(main())
