#!/usr/bin/env python3
"""Media migration STEP 2 — transfer every blob referenced by the scan manifest
from Supabase Storage to the self-hosted storage service (ADR-054).

Reference-driven: only blobs the DB actually points at are moved (orphans die
with Supabase). Deduped by (bucket, key) — a blob referenced from N cells is
transferred once. Idempotent/resumable: default PUT `upsert=false`, a 409
ALREADY_EXISTS counts as success (the post-cutover storage-service copy wins).
Signed/plain URL variants of the same key transfer through the same
service-role GET.

Usage (from `ai-storybook-swap-service/`, envs from `.env`):
    uv run python scripts/media_migration_2_transfer_blobs.py --dry-run
    uv run python scripts/media_migration_2_transfer_blobs.py
    uv run python scripts/media_migration_2_transfer_blobs.py --limit 20 --workers 4

Input : scripts/media-migration-output/scan-manifest.json
Output: scripts/media-migration-output/transfer-results.json
  results: {"{bucket}/{key}": {status: migrated|exists|invalid_key|failed,
                               new_url?, reason?, bytes?}}
Exit 0 = no hard failures; 1 = at least one failed blob (fix + re-run).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_migration_lib import (  # noqa: E402
    SCAN_MANIFEST, TRANSFER_RESULTS, StoragePutError, load_json, new_public_url,
    save_json, storage_service_put, supabase_download,
)
from src.config.settings import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("media-transfer")

_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=30.0)


async def transfer_blob(
    sem: asyncio.Semaphore,
    dl_client: httpx.AsyncClient,
    put_client: httpx.AsyncClient,
    blob: dict,
    upsert: bool,
) -> dict:
    bucket, key = blob["bucket"], blob["key"]
    async with sem:
        try:
            body, content_type = await supabase_download(
                dl_client, settings.app_storage_url, settings.app_storage_service_key,
                bucket, key,
            )
        except httpx.HTTPStatusError as exc:
            return {"status": "failed", "reason": f"download {exc.response.status_code}"}
        except httpx.HTTPError as exc:
            return {"status": "failed", "reason": f"download {type(exc).__name__}: {exc}"}

        try:
            data = await storage_service_put(
                put_client, settings.storage_service_url,
                settings.storage_service_api_key,
                bucket, key, body, content_type, upsert,
            )
        except StoragePutError as exc:
            if exc.status == 409:
                return {
                    "status": "exists",
                    "new_url": new_public_url(settings.storage_public_base_url, bucket, key),
                }
            return {"status": "failed", "reason": f"put {exc.status} {exc.code}"}
        except httpx.HTTPError as exc:
            return {"status": "failed", "reason": f"put {type(exc).__name__}: {exc}"}

        if data.get("bytes") != len(body):
            return {
                "status": "failed",
                "reason": f"size mismatch put={data.get('bytes')} src={len(body)}",
            }
        return {"status": "migrated", "new_url": data["url"], "bytes": len(body)}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(SCAN_MANIFEST))
    parser.add_argument("--out", default=str(TRANSFER_RESULTS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert", action="store_true", help="overwrite existing objects")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="stop after N blobs")
    args = parser.parse_args()

    if not settings.storage_service_url:
        logger.error("STORAGE_SERVICE_URL is not set — nothing to migrate into")
        return 1
    if not settings.app_storage_url or not settings.app_storage_service_key:
        logger.error("APP_STORAGE_URL / APP_STORAGE_SERVICE_KEY missing — cannot download")
        return 1

    manifest = load_json(Path(args.manifest))
    blobs: dict[str, dict] = manifest["blobs"]

    # Carry forward previous results so re-runs only touch unfinished blobs.
    previous: dict[str, dict] = {}
    out_path = Path(args.out)
    if out_path.exists():
        previous = load_json(out_path).get("results", {})

    results: dict[str, dict] = {}
    todo: dict[str, dict] = {}
    for bid, blob in blobs.items():
        prev = previous.get(bid)
        if not blob["valid"]:
            results[bid] = {"status": "invalid_key", "reason": blob["reason"]}
        elif prev and prev.get("status") in ("migrated", "exists"):
            results[bid] = prev
        else:
            todo[bid] = blob
    if args.limit:
        todo = dict(list(todo.items())[: args.limit])

    logger.info(
        "blobs=%d todo=%d done_before=%d invalid=%d dry_run=%s",
        len(blobs), len(todo),
        sum(1 for r in results.values() if r["status"] in ("migrated", "exists")),
        sum(1 for r in results.values() if r["status"] == "invalid_key"),
        args.dry_run,
    )

    if not args.dry_run and todo:
        sem = asyncio.Semaphore(args.workers)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as dl_client, \
                httpx.AsyncClient(timeout=_TIMEOUT) as put_client:
            async def run(bid: str, blob: dict) -> None:
                results[bid] = await transfer_blob(sem, dl_client, put_client, blob, args.upsert)

            done = 0
            for coro in asyncio.as_completed([run(b, v) for b, v in todo.items()]):
                await coro
                done += 1
                if done % 50 == 0 or done == len(todo):
                    logger.info("progress %d/%d", done, len(todo))

    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    failed = [
        {"blob": bid, **r} for bid, r in results.items() if r["status"] == "failed"
    ]
    save_json(out_path, {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "dry_run": args.dry_run,
        "counts": counts,
        "failed": failed,
        "results": results,
    })
    logger.info("DONE %s → %s", counts, out_path)
    for f in failed[:10]:
        logger.warning("failed: %s (%s)", f["blob"], f["reason"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
