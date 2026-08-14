#!/usr/bin/env python3
"""Media migration STEP 1 — scan the whole App DB for old Supabase Storage URLs.

Sweeps EVERY text/varchar/json/jsonb column of every BASE TABLE in the `public`
schema (information_schema-driven, per user spec "search toàn bộ DB" — nothing
can be missed by a stale allowlist) and records each URL string together with
the exact cell it lives in. Read-only; safe to run any time. Re-run after
STEP 3 as the VERIFY pass — residue must be 0 (or explained).

Usage (from `ai-storybook-swap-service/`, envs from `.env`):
    uv run python scripts/media_migration_1_scan_db_urls.py
    uv run python scripts/media_migration_1_scan_db_urls.py --tables snapshots,remixes

Output: scripts/media-migration-output/scan-manifest.json
  occurrences[]: {table, pk: {col: value}, pk_types, column, col_type, urls[]}
  blobs: {"{bucket}/{key}": {bucket, key, valid, reason, url_count}}
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_migration_lib import (  # noqa: E402
    SCAN_MANIFEST, STORAGE_URL_RE, blob_id, parse_storage_url, save_json,
)
from src.config.settings import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("media-scan")

_SCANNABLE_TYPES = ("text", "character varying", "json", "jsonb")

_COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
  AND c.data_type = ANY($1::text[])
ORDER BY c.table_name, c.ordinal_position
"""

_PK_SQL = """
SELECT kcu.table_name, kcu.column_name, c.data_type
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
JOIN information_schema.columns c
  ON c.table_schema = kcu.table_schema AND c.table_name = kcu.table_name
 AND c.column_name = kcu.column_name
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
ORDER BY kcu.table_name, kcu.ordinal_position
"""


def _qi(identifier: str) -> str:
    """Quote an identifier coming from information_schema (trusted, still quoted)."""
    return '"' + identifier.replace('"', '""') + '"'


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tables", help="CSV filter: only scan these tables")
    parser.add_argument("--out", default=str(SCAN_MANIFEST))
    args = parser.parse_args()
    table_filter = (
        {t.strip() for t in args.tables.split(",") if t.strip()} if args.tables else None
    )

    conn = await asyncpg.connect(settings.app_db_url)
    try:
        pk_rows = await conn.fetch(_PK_SQL)
        pks: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in pk_rows:
            pks[r["table_name"]].append((r["column_name"], r["data_type"]))

        col_rows = await conn.fetch(_COLUMNS_SQL, list(_SCANNABLE_TYPES))
        occurrences: list[dict] = []
        blobs: dict[str, dict] = {}
        no_pk_tables: set[str] = set()
        cells_scanned = 0

        for r in col_rows:
            table, column, col_type = r["table_name"], r["column_name"], r["data_type"]
            if table_filter and table not in table_filter:
                continue
            pk_cols = pks.get(table)
            if not pk_cols:
                no_pk_tables.add(table)
                continue
            pk_select = ", ".join(_qi(c) for c, _ in pk_cols)
            sql = (
                f"SELECT {pk_select}, {_qi(column)}::text AS cell "
                f"FROM {_qi(table)} "
                f"WHERE {_qi(column)}::text LIKE '%/storage/v1/object/%'"
            )
            rows = await conn.fetch(sql)
            if rows:
                logger.info("hit table=%s column=%s rows=%d", table, column, len(rows))
            for row in rows:
                cells_scanned += 1
                urls = sorted(set(STORAGE_URL_RE.findall(row["cell"])))
                if not urls:
                    continue  # marker present but regex-unmatchable → residue check
                occurrences.append({
                    "table": table,
                    "pk": {c: row[c] for c, _ in pk_cols},
                    "pk_types": {c: t for c, t in pk_cols},
                    "column": column,
                    "col_type": col_type,
                    "urls": urls,
                })
                for url in urls:
                    parsed = parse_storage_url(url)
                    bid = blob_id(parsed["bucket"], parsed["key"])
                    entry = blobs.setdefault(bid, {**parsed, "url_count": 0})
                    entry["url_count"] += 1
    finally:
        await conn.close()

    invalid = {k: v for k, v in blobs.items() if not v["valid"]}
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_host": settings.app_db_url.rsplit("@", 1)[-1],  # no credentials
        "tables_filter": sorted(table_filter) if table_filter else None,
        "cells_with_urls": cells_scanned,
        "occurrence_count": len(occurrences),
        "unique_blob_count": len(blobs),
        "invalid_blob_count": len(invalid),
        "tables_without_pk_skipped": sorted(no_pk_tables),
        "occurrences": occurrences,
        "blobs": blobs,
    }
    out = Path(args.out)
    save_json(out, manifest)
    logger.info(
        "DONE occurrences=%d unique_blobs=%d invalid_keys=%d no_pk_tables=%s → %s",
        len(occurrences), len(blobs), len(invalid),
        sorted(no_pk_tables) or "-", out,
    )
    if invalid:
        for bid, v in list(invalid.items())[:10]:
            logger.warning("invalid key (%s): %s", v["reason"], bid)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
