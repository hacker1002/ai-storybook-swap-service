#!/usr/bin/env python3
"""Media migration STEP 3 — rewrite old Supabase URLs in the DB to the new
storage-service URLs, for blobs that STEP 2 transferred successfully.

DEFAULT IS DRY-RUN (counts only). Pass `--apply` to write.

Concurrency-safe by design: never a read-modify-write from the manifest cell
value. Each rewrite is one targeted, atomic SQL statement doing an in-place
substring replace on the CURRENT cell content:

    UPDATE "t" SET "col" = replace("col"::text, $old, $new)::jsonb
    WHERE "pk" = $pk AND strpos("col"::text, $old) > 0;

A row edited between scan and rewrite is safe (replace runs on live content);
a URL that vanished since the scan simply no-matches (counted, not an error).
`strpos` (not LIKE) so `_`/`%` in URLs can't wildcard-match. URLs whose blob
failed/invalid in STEP 2 are left untouched — old links keep working while
Supabase Storage is still up; fix, re-run STEP 2, then re-run this.

Usage (from `ai-storybook-swap-service/`, envs from `.env`):
    uv run python scripts/media_migration_3_rewrite_db_urls.py            # dry-run
    uv run python scripts/media_migration_3_rewrite_db_urls.py --apply
    uv run python scripts/media_migration_3_rewrite_db_urls.py --apply --tables snapshots

Inputs : scan-manifest.json + transfer-results.json
Output : scripts/media-migration-output/rewrite-report.json
Verify : re-run STEP 1 afterwards — residue must be 0 or explained.
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
    REWRITE_REPORT, SCAN_MANIFEST, TRANSFER_RESULTS, blob_id, load_json,
    parse_storage_url, save_json,
)
from src.config.settings import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("media-rewrite")

_CASTS = {"jsonb": "::jsonb", "json": "::json"}  # text/varchar need no cast


def _qi(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _build_sql(occ: dict, apply: bool) -> str:
    table, column = _qi(occ["table"]), _qi(occ["column"])
    cast = _CASTS.get(occ["col_type"], "")
    pk_cols = list(occ["pk"].keys())
    where_pk = " AND ".join(
        f"{_qi(c)} = ${i + 3}::{occ['pk_types'][c]}" for i, c in enumerate(pk_cols)
    )
    guard = f"strpos({column}::text, $1) > 0"
    if apply:
        return (
            f"UPDATE {table} SET {column} = replace({column}::text, $1, $2){cast} "
            f"WHERE {where_pk} AND {guard}"
        )
    # dry-run probe: $2 unused but kept so the param list is identical
    return f"SELECT ($2 = $2) FROM {table} WHERE {where_pk} AND {guard}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(SCAN_MANIFEST))
    parser.add_argument("--transfer", default=str(TRANSFER_RESULTS))
    parser.add_argument("--out", default=str(REWRITE_REPORT))
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    parser.add_argument("--tables", help="CSV filter: only rewrite these tables")
    args = parser.parse_args()
    table_filter = (
        {t.strip() for t in args.tables.split(",") if t.strip()} if args.tables else None
    )

    manifest = load_json(Path(args.manifest))
    transfer = load_json(Path(args.transfer))["results"]

    per_table: dict[str, dict[str, int]] = defaultdict(
        lambda: {"updated": 0, "not_matched": 0, "skipped_url": 0}
    )
    skipped_urls: dict[str, str] = {}
    errors: list[dict] = []

    conn = await asyncpg.connect(settings.app_db_url)
    try:
        for occ in manifest["occurrences"]:
            if table_filter and occ["table"] not in table_filter:
                continue
            stats = per_table[occ["table"]]
            for old_url in occ["urls"]:
                parsed = parse_storage_url(old_url)
                bid = blob_id(parsed["bucket"], parsed["key"])
                result = transfer.get(bid) or {"status": "missing"}
                if result.get("status") not in ("migrated", "exists"):
                    stats["skipped_url"] += 1
                    skipped_urls[old_url] = result.get("status", "missing")
                    continue
                new_url = result["new_url"]
                pk_values = [str(occ["pk"][c]) for c in occ["pk"]]
                sql = _build_sql(occ, args.apply)
                try:
                    if args.apply:
                        status = await conn.execute(sql, old_url, new_url, *pk_values)
                        rowcount = int(status.rsplit(" ", 1)[-1])
                    else:
                        row = await conn.fetchrow(sql, old_url, new_url, *pk_values)
                        rowcount = 1 if row else 0
                    if rowcount:
                        stats["updated"] += 1
                    else:
                        stats["not_matched"] += 1
                except asyncpg.PostgresError as exc:
                    errors.append({
                        "table": occ["table"], "pk": occ["pk"],
                        "column": occ["column"], "url": old_url, "error": str(exc),
                    })
    finally:
        await conn.close()

    totals = {
        k: sum(t[k] for t in per_table.values())
        for k in ("updated", "not_matched", "skipped_url")
    }
    save_json(Path(args.out), {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": args.apply,
        "db_host": settings.app_db_url.rsplit("@", 1)[-1],
        "totals": totals,
        "per_table": dict(per_table),
        "skipped_urls": skipped_urls,
        "errors": errors,
    })
    label = "APPLIED" if args.apply else "DRY-RUN (pass --apply to write)"
    logger.info(
        "DONE [%s] updated=%d not_matched=%d skipped_url=%d errors=%d → %s",
        label, totals["updated"], totals["not_matched"], totals["skipped_url"],
        len(errors), args.out,
    )
    if not args.apply:
        for table, s in sorted(per_table.items()):
            logger.info("  %-24s would_update=%d skipped=%d", table, s["updated"], s["skipped_url"])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
