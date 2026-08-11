"""PostgresAppDbAdapter — the only place raw SQL lives.

Invariants:
  - Every VALUE goes through a `$n` placeholder. Column names come ONLY from
    hard-coded allowlists (`INSERT_COLUMNS`, `WRITABLE_REMIX_COLUMNS`) — never
    interpolated from request data.
  - Each method acquires a connection for a SINGLE statement and releases it.
  - Returns plain dict/list[dict] (JSONB already decoded by the pool codec) — never
    an `asyncpg.Record`.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from src.core.remix_columns import INSERT_COLUMNS, WRITABLE_REMIX_COLUMNS

_ACTIVE_JOB_STATUSES = ("queued", "running")

# Identifier allowlists for the dynamic-column writers (P3b callers). Mirrors the
# remix invariant: column names come ONLY from these frozensets, never from a
# caller-supplied dict key — even though no request path reaches them yet.
_UPDATABLE_JOB_COLUMNS: frozenset[str] = frozenset(
    {"status", "current_step", "total_steps", "step_details", "result", "error_message",
     "cancel_requested", "book_id", "params"}
)
_AI_LOG_COLUMNS: frozenset[str] = frozenset(
    {"provider", "operation", "model", "status", "error", "latency_ms", "book_id",
     "snapshot_id", "remix_id", "job_id", "user_id", "request", "response",
     "provider_request_id", "input_tokens", "output_tokens", "total_tokens",
     "usage_unit", "usage_amount", "cost_usd", "cost_source", "pricing_version"}
)


class PostgresAppDbAdapter:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ reads
    async def get_book(self, book_id: UUID) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM books WHERE id = $1", book_id)
        return dict(row) if row else None

    async def get_current_snapshot(self, book_id: UUID, current_version: UUID | None) -> dict | None:
        """Mirror editor `fetchSnapshot`: resolve by `books.current_version`; when
        NULL, fall back to the latest snapshot for the book by `updated_at DESC`
        (verified against ai-storybook-editor/src/stores/snapshot-store/index.ts)."""
        async with self._pool.acquire() as conn:
            if current_version is not None:
                row = await conn.fetchrow("SELECT * FROM snapshots WHERE id = $1", current_version)
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM snapshots WHERE book_id = $1 ORDER BY updated_at DESC LIMIT 1",
                    book_id,
                )
        return dict(row) if row else None

    async def get_art_style(self, art_style_id: UUID) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM art_styles WHERE id = $1", art_style_id)
        return dict(row) if row else None

    async def list_humans(self, book_id: UUID) -> list[dict]:
        """`humans` has NO book_id column — the editor loads them GLOBALLY
        (humans-store.ts fetchHumans = SELECT * ORDER BY created_at DESC, RLS
        permissive). Parameter kept for a stable README §7 signature; intentionally
        unused (not a bug)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM humans ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    async def list_voices(self, book_id: UUID) -> list[dict]:
        """`voices` also has NO book_id — global load, mirrors humans (parity)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM voices ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- remixes
    async def list_remixes(self, snapshot_id: UUID) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM remixes WHERE snapshot_id = $1 ORDER BY created_at DESC",
                snapshot_id,
            )
        return [dict(r) for r in rows]

    async def get_remix(self, remix_id: UUID) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM remixes WHERE id = $1", remix_id)
        return dict(row) if row else None

    async def snapshot_exists(self, snapshot_id: UUID) -> bool:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1 FROM snapshots WHERE id = $1", snapshot_id)
        return val is not None

    async def insert_remix(self, row: dict) -> dict:
        """INSERT from the hard-coded `INSERT_COLUMNS` allowlist. Keys absent from
        `row` are simply omitted (DDL/normalized defaults apply). Column names are
        constant; every value is a placeholder."""
        cols = [c for c in INSERT_COLUMNS if c in row]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        col_list = ", ".join(cols)
        values = [row[c] for c in cols]
        sql = f"INSERT INTO remixes ({col_list}) VALUES ({placeholders}) RETURNING *"
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(sql, *values)
        return dict(record)

    async def update_remix_columns(self, remix_id: UUID, columns: dict) -> bool:
        """UPDATE only allowlisted columns. Defense-in-depth: the router already
        rejects non-writable keys; here we raise if one slips through (never build
        SQL from an unknown name)."""
        bad = set(columns) - WRITABLE_REMIX_COLUMNS
        if bad:
            raise ValueError(f"non-writable column reached adapter: {sorted(bad)}")
        if not columns:
            raise ValueError("update_remix_columns called with empty columns")
        cols = list(columns)
        set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        values = [columns[c] for c in cols]
        sql = f"UPDATE remixes SET {set_clause}, updated_at = now() WHERE id = $1"
        async with self._pool.acquire() as conn:
            result = await conn.execute(sql, remix_id, *values)
        return _rowcount(result) > 0

    async def delete_remix(self, remix_id: UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM remixes WHERE id = $1", remix_id)
        return _rowcount(result) > 0

    async def get_book_id_for_remix(self, remix_id: UUID) -> UUID | None:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT s.book_id FROM remixes r JOIN snapshots s ON s.id = r.snapshot_id WHERE r.id = $1",
                remix_id,
            )
        return val

    # ------------------------------------------------------------------ jobs
    async def get_jobs(self, ids: list[UUID]) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, type, status, step_details, result, error_message,
                          cancel_requested, params, book_id, current_step, total_steps, updated_at
                   FROM background_jobs WHERE id = ANY($1::uuid[])""",
                ids,
            )
        return [dict(r) for r in rows]

    async def find_active_job(self, remix_id: UUID, job_type: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM background_jobs
                   WHERE type = $1 AND status = ANY($2::text[]) AND params->>'remix_id' = $3
                   ORDER BY created_at DESC LIMIT 1""",
                job_type,
                list(_ACTIVE_JOB_STATUSES),
                str(remix_id),
            )
        return dict(row) if row else None

    async def has_active_job(self, remix_id: UUID) -> bool:
        """Any-type active-job guard for delete (spec 06 REMIX_BUSY)."""
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                """SELECT 1 FROM background_jobs
                   WHERE status = ANY($1::text[]) AND params->>'remix_id' = $2 LIMIT 1""",
                list(_ACTIVE_JOB_STATUSES),
                str(remix_id),
            )
        return val is not None

    async def insert_job(self, row: dict) -> dict:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """INSERT INTO background_jobs
                       (type, user_id, book_id, status, params, step_details, total_steps)
                   VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
                row["type"],
                row["user_id"],
                row.get("book_id"),
                row.get("status", "queued"),
                row.get("params", {}),
                row.get("step_details", {}),
                row.get("total_steps", 1),
            )
        return dict(record)

    async def update_job(self, job_id: UUID, fields: dict, expect_status: str | None = None) -> bool:
        if not fields:
            raise ValueError("update_job called with empty fields")
        bad = set(fields) - _UPDATABLE_JOB_COLUMNS
        if bad:
            raise ValueError(f"non-updatable job column: {sorted(bad)}")
        cols = list(fields)
        set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        values = [fields[c] for c in cols]
        sql = f"UPDATE background_jobs SET {set_clause}, updated_at = now() WHERE id = $1"
        params: list = [job_id, *values]
        if expect_status is not None:
            sql += f" AND status = ${len(params) + 1}"
            params.append(expect_status)
        async with self._pool.acquire() as conn:
            result = await conn.execute(sql, *params)
        return _rowcount(result) > 0

    async def insert_ai_log(self, row: dict) -> None:
        bad = set(row) - _AI_LOG_COLUMNS
        if bad:
            raise ValueError(f"non-allowlisted ai_service_logs column: {sorted(bad)}")
        cols = list(row)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        sql = f"INSERT INTO ai_service_logs ({', '.join(cols)}) VALUES ({placeholders})"
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *[row[c] for c in cols])


def _rowcount(command_tag: str) -> int:
    """Parse asyncpg's command tag (e.g. 'UPDATE 1', 'DELETE 0') into an int."""
    try:
        return int(command_tag.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0
