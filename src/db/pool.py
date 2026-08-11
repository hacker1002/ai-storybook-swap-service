"""asyncpg connection pool lifecycle + JSONB codec.

Rules (README §7):
  - acquire per-query, release immediately — NEVER hold a connection across an AI
    call (P3b adds 30-90s Gemini/Replicate calls). Enforced by adapter methods
    each wrapping a single `async with pool.acquire()`.
  - `statement_timeout` set at pool level via server_settings.
  - asyncpg returns jsonb as `str` by default → register a codec so JSONB round-
    trips as native dict/list.
"""

from __future__ import annotations

import json

import asyncpg

from src.config.settings import settings
from src.core.logging import get_logger

logger = get_logger("db.pool")

_POOL: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codecs so values decode to dict/list and encode from them."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def create_pool() -> asyncpg.Pool:
    """Create the module-global pool. Idempotent — returns the existing pool if set."""
    global _POOL
    if _POOL is not None:
        return _POOL
    _POOL = await asyncpg.create_pool(
        dsn=settings.app_db_url,
        min_size=settings.app_db_pool_min,
        max_size=settings.app_db_pool_max,
        init=_init_connection,
        server_settings={"statement_timeout": str(settings.app_db_statement_timeout_ms)},
    )
    # Log host/db only — never the DSN (carries credentials).
    logger.info("pool_created", extra={"data": {"min": settings.app_db_pool_min, "max": settings.app_db_pool_max}})
    return _POOL


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
        logger.info("pool_closed")


def get_pool() -> asyncpg.Pool:
    if _POOL is None:
        raise RuntimeError("DB pool not initialized — create_pool() must run in lifespan startup")
    return _POOL
