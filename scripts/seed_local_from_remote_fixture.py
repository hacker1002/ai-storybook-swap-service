#!/usr/bin/env python3
"""DEV ONLY — load the REAL remote-DB fixtures (book/snapshot/remix/humans/voices,
captured under test-scripts/fixtures/remote-*.json) INTO the local Supabase DB so
the P3b job/remix live test-scripts run against realistically-shaped data.

Why: the local DB had no remix with a `mixes[]` batch, so mix-swap/rmbg/upscale/
detect-mix/detect-rmbg job scripts 404'd on `batch_id`. Remix
6e25c876-4977-4e1f-b409-3462821bc53d DOES carry mixes[2]/sprites[1]/rmbgs[3]/
upscales[1] — captured verbatim and upserted here.

Order respects FKs: book -> snapshot (snapshots.book_id FK) -> remix
(remixes.snapshot_id FK); humans/voices are global (no book FK). Upsert is
ON CONFLICT (id) DO UPDATE so re-running is idempotent. `created_at`/`updated_at`
are dropped (DB defaults apply, avoids timestamptz-from-string). On a
ForeignKeyViolation (a referenced art_style/user/project row absent locally) the
offending column is set NULL and the insert retried; if that column is NOT NULL the
script stops and names it so you can seed the parent row.

Usage: uv run --with asyncpg python scripts/seed_local_from_remote_fixture.py
Exit: 0 seeded (writes test-scripts/fixtures/local-ids.env) · 2 cannot connect ·
      1 a NOT NULL FK column blocked the insert (named in output).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys

import asyncpg

_DSN = os.environ.get("APP_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
_FIX = pathlib.Path(__file__).resolve().parent.parent / "test-scripts" / "fixtures"
_DROP_COLS = {"created_at", "updated_at"}  # let DB defaults win; avoid tz parsing
_FK_COL_RE = re.compile(r"Key \((?P<col>[^)]+)\)")


def _load(name: str):
    p = _FIX / name
    if not p.exists():
        raise SystemExit(f"❌ missing fixture {p} — run the supabase-query capture first")
    return json.loads(p.read_text())


async def _register_json_codecs(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def _local_owner(conn: asyncpg.Connection) -> str | None:
    """A real local auth.users id to stand in for the remote owner (which won't
    exist locally). Prefer REMIX_SWAP_SERVICE_USER_ID, else the first user row."""
    uid = os.environ.get("REMIX_SWAP_SERVICE_USER_ID") or ""
    if uid:
        exists = await conn.fetchval("SELECT 1 FROM auth.users WHERE id = $1::uuid", uid)
        if exists:
            return uid
    return await conn.fetchval("SELECT id::text FROM auth.users ORDER BY created_at LIMIT 1")


# Owner/user columns are NOT NULL FKs -> auth.users; remap to a local user so the
# insert doesn't dangle. Values are irrelevant to job logic (dev fixtures).
_OWNER_COLS = {"owner_id", "user_id"}


async def _upsert(conn: asyncpg.Connection, table: str, row: dict, owner: str | None) -> None:
    """Insert one row (ON CONFLICT id DO UPDATE). Remaps owner/user columns to a
    local user; self-heals nullable FK misses by NULLing the offending column and
    retrying; raises on a NOT NULL blocker it can't satisfy."""
    row = {k: v for k, v in row.items() if k not in _DROP_COLS}
    if owner:
        for c in _OWNER_COLS:
            if c in row:
                row[c] = owner
    nulled: set[str] = set()
    while True:
        cols = list(row)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}"
        )
        try:
            await conn.execute(sql, *[row[c] for c in cols])
            if nulled:
                print(f"   · {table}: nulled absent-FK columns {sorted(nulled)}")
            return
        except asyncpg.ForeignKeyViolationError as exc:
            m = _FK_COL_RE.search(str(exc.detail or exc))
            col = m.group("col") if m else None
            if not col or col not in row or col in nulled:
                raise
            row[col] = None
            nulled.add(col)
        except asyncpg.NotNullViolationError as exc:
            raise SystemExit(
                f"❌ {table}: a NOT NULL column blocks the insert ({exc.column_name or exc}). "
                f"Seed its parent row first, then retry."
            ) from exc


async def main() -> int:
    book = _load("remote-book.json")
    snapshot = _load("remote-snapshot.json")
    remix = _load("remote-remix.json")
    humans = _load("remote-humans.json")
    voices = _load("remote-voices.json")

    try:
        conn = await asyncpg.connect(_DSN)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Cannot connect to local App DB at {_DSN}\n   {type(exc).__name__}: {exc}")
        print("   Start local Supabase (or set APP_DB_URL) and retry.")
        return 2

    try:
        await _register_json_codecs(conn)
        owner = await _local_owner(conn)
        if not owner:
            print("⚠️  no auth.users row locally — owner_id NOT NULL FKs will fail. "
                  "Create a user (supabase) or set REMIX_SWAP_SERVICE_USER_ID.")
        # FK order: book -> snapshot -> remix; humans/voices are global.
        await _upsert(conn, "books", book, owner)
        await _upsert(conn, "snapshots", snapshot, owner)
        for h in humans:
            await _upsert(conn, "humans", h, owner)
        for v in voices:
            await _upsert(conn, "voices", v, owner)
        await _upsert(conn, "remixes", remix, owner)
        # Re-point current_version now that the snapshot row exists (it was NULLed
        # during the book insert since the FK target didn't exist yet). Deterministic
        # get_current_snapshot resolution for the live scripts.
        await conn.execute(
            "UPDATE books SET current_version = $1 WHERE id = $2", snapshot["id"], book["id"]
        )
    finally:
        await conn.close()

    def _first_id(lst) -> str:
        return str(lst[0]["id"]) if isinstance(lst, list) and lst and lst[0].get("id") else ""

    ids = {
        "BOOK_ID": book["id"],
        "SNAPSHOT_ID": snapshot["id"],
        "REMIX_ID": remix["id"],
        "BATCH_ID": _first_id(remix.get("mixes")),          # mix-swap / detect-mix
        "SPRITE_ID": _first_id(remix.get("sprites")),        # sprite-swap / detect-sprite
        "RMBG_BATCH_ID": _first_id(remix.get("rmbgs")),      # detect-rmbg
        "UPSCALE_BATCH_ID": _first_id(remix.get("upscales")),
        "JOB_ID": "",
    }
    out = _FIX / "local-ids.env"
    out.write_text(
        "# DEV fixture ids (gitignored) — seeded from remote by "
        "seed_local_from_remote_fixture.py\n"
        + "".join(f"{k}={v}\n" for k, v in ids.items())
    )
    print(f"✅ Seeded local DB + wrote {out}")
    for k, v in ids.items():
        print(f"   {k}={v or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
