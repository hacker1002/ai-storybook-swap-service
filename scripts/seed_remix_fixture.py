#!/usr/bin/env python3
"""DEV ONLY — locate a local-DB remix that carries real batch/sprite data
(mixes[] / sprites[]) so the P3b job test-scripts have enqueue-able ids, then
export them to test-scripts/fixtures/local-ids.env (gitignored).

Extends discover_fixture_ids.py (which only finds book/snapshot/remix) with the
extra ids the job routes need: BATCH_ID (mixes[].id) and SPRITE_ID (sprites[].id).
remix -> book is bridged via remixes.snapshot_id -> snapshots.book_id (the
remixes table has no book_id column — see memory note).

Usage: uv run --with asyncpg python scripts/seed_remix_fixture.py

Exit codes:
  0  fixtures written (a remix with mixes[] or sprites[] was found)
  1  DB reachable but NO qualifying remix / no book — prints exactly what is missing
  2  cannot connect to the local DB
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import asyncpg

_DSN = os.environ.get("APP_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
_OUT = pathlib.Path(__file__).resolve().parent.parent / "test-scripts" / "fixtures" / "local-ids.env"


def _first_id(raw) -> str:
    """Return the .id of the first element of a JSONB array column, else ''."""
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return ""
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return str(raw[0].get("id") or "")
    return ""


async def main() -> int:
    try:
        conn = await asyncpg.connect(_DSN)
    except Exception as exc:  # noqa: BLE001 — surface the connection failure verbatim
        print(f"❌ Cannot connect to local App DB at {_DSN}")
        print(f"   {type(exc).__name__}: {exc}")
        print("   Start the local Supabase/Postgres stack (or set APP_DB_URL) and retry.")
        return 2

    try:
        # A remix with real crop data: mixes[] non-empty (batch) OR sprites[] non-empty.
        remix = await conn.fetchrow(
            """
            SELECT id, snapshot_id, mixes, sprites
            FROM remixes
            WHERE COALESCE(jsonb_array_length(mixes), 0) > 0
               OR COALESCE(jsonb_array_length(sprites), 0) > 0
            ORDER BY COALESCE(jsonb_array_length(mixes), 0)
                   + COALESCE(jsonb_array_length(sprites), 0) DESC
            LIMIT 1
            """
        )
        if remix is None:
            n_remixes = await conn.fetchval("SELECT count(*) FROM remixes")
            print("❌ No remix with batch/sprite data found on the local DB.")
            print(f"   remixes rows total: {n_remixes}")
            print("   Need a remix with a non-empty mixes[] (batch) or sprites[] array.")
            print("   Create/seed one via the editor or the remix creation flow, then retry.")
            print("   (Refusing to fabricate BATCH_ID/SPRITE_ID — job enqueue would 404/422.)")
            return 1

        remix_id = remix["id"]
        snapshot_id = remix["snapshot_id"]
        batch_id = _first_id(remix["mixes"])
        sprite_id = _first_id(remix["sprites"])

        # remix -> book via the snapshot bridge (remixes has no book_id).
        book_id = ""
        if snapshot_id is not None:
            book_id = await conn.fetchval(
                "SELECT book_id FROM snapshots WHERE id = $1", snapshot_id
            ) or ""

        job = await conn.fetchrow("SELECT id FROM background_jobs ORDER BY created_at DESC LIMIT 1")
        job_id = job["id"] if job else ""
    finally:
        await conn.close()

    missing = []
    if not batch_id:
        missing.append("BATCH_ID (remix has no mixes[] — mix/rmbg/upscale jobs will 404)")
    if not sprite_id:
        missing.append("SPRITE_ID (remix has no sprites[] — sprite-swap job will 404)")

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(
        "# DEV fixture ids (gitignored) — regenerate with seed_remix_fixture.py\n"
        f"BOOK_ID={book_id}\n"
        f"SNAPSHOT_ID={snapshot_id or ''}\n"
        f"REMIX_ID={remix_id}\n"
        f"BATCH_ID={batch_id}\n"
        f"SPRITE_ID={sprite_id}\n"
        f"JOB_ID={job_id}\n"
    )
    print(f"✅ Wrote {_OUT}")
    print(f"   BOOK_ID={book_id or '(none)'}")
    print(f"   SNAPSHOT_ID={snapshot_id or '(none)'}")
    print(f"   REMIX_ID={remix_id}")
    print(f"   BATCH_ID={batch_id or '(none)'}")
    print(f"   SPRITE_ID={sprite_id or '(none)'}")
    print(f"   JOB_ID={job_id or '(none)'}")

    if missing:
        # Fixture WAS written and at least one crop id is present (the WHERE clause
        # guarantees mixes[] or sprites[] is non-empty) — usable, so exit 0 with a
        # clear warning. Non-zero is reserved for "can't connect" / "no qualifying
        # remix", never a partial-but-usable fixture.
        print("\n⚠️  Partial fixture — the following job scripts cannot fully run:")
        for m in missing:
            print(f"     - {m}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
