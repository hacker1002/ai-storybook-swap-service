"""Reaper — sweeps stale running/queued jobs.

Ported from `ai-storybook-image-api/src/jobs/reaper.py`. The image-api version
issued TWO bulk conditional `UPDATE ... RETURNING` calls (PostgREST). asyncpg has
no single-statement bulk-CAS-with-representation, so the equivalent here is:

  1. `adapter.list_stale_jobs(running_cutoff, queued_cutoff)` — SELECT the stale rows.
  2. Per row: `adapter.update_job(id, {failed…}, expect_status=<row status>)` — a
     CAS flip. Only a WON CAS (rowcount 1) runs the finalize hook, so a worker /
     another reaper instance finalizing the same row concurrently loses cleanly
     (rowcount 0 → skip). The status-guarded CAS is what makes multiple instances
     safe (risk table: "Reaper nhiều instance tranh nhau").

Thresholds unchanged: INTERVAL 30s / running-stale 1800s / queued-stale 300s.
Idempotent: a row a worker already finalized (terminal status) is either not
returned by the SELECT or loses the CAS. Every winning UPDATE bumps `updated_at`
(the adapter stamps it) for audit consistency.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.db.adapter import get_adapter
from src.jobs.config import (
    REAPER_INTERVAL_SEC,
    REAPER_QUEUED_STALE_SEC,
    REAPER_STALE_SEC,
)
from src.jobs.runner import _run_finalize

logger = logging.getLogger(__name__)


async def _reap_once(stale_after_sec: int, queued_stale_sec: int) -> tuple[int, int]:
    """Run ONE sweep. Returns (running_swept, queued_swept). Extracted from the
    loop so the sweep is directly unit-testable without driving the infinite
    `reaper_loop`; the logic is otherwise identical to image-api's iteration body.
    """
    now = datetime.now(tz=timezone.utc)
    running_cutoff = now - timedelta(seconds=stale_after_sec)
    queued_cutoff = now - timedelta(seconds=queued_stale_sec)

    stale = await get_adapter().list_stale_jobs(running_cutoff, queued_cutoff)

    swept_running = 0
    swept_queued = 0
    for row in stale:
        prev_status = row.get("status")
        message = "stale worker" if prev_status == "running" else "spawn race"
        result_payload = {"errors": [{"stage": "internal", "message": message}]}

        # CAS flip guarded on the row's CURRENT status — a worker (or another
        # reaper instance) that already finalized this row wins, and this UPDATE
        # matches 0 rows → we skip the finalize (no double-fire).
        won = await get_adapter().update_job(
            row["id"], {"status": "failed", "result": result_payload}, expect_status=prev_status
        )
        if not won:
            continue

        if prev_status == "running":
            swept_running += 1
        else:
            swept_queued += 1

        # Finalize-hook for swept rows that own side state (e.g. export_pdf →
        # distribution leaf). The SELECT returned the full row, so it already
        # carries id/type/params/result — no extra query. `_run_finalize` is a
        # no-op for types without a hook. Per-row try/except so one bad row never
        # breaks the sweep.
        try:
            await _run_finalize(row, "failed", result_payload)
        except Exception:  # noqa: BLE001 — never let one row kill the sweep
            logger.exception("reaper_finalize_row_error id=%s", row.get("id"))

    if swept_running or swept_queued:
        logger.warning("reaper_swept running=%d queued=%d", swept_running, swept_queued)
    return swept_running, swept_queued


async def reaper_loop(
    interval_sec: int = REAPER_INTERVAL_SEC,
    stale_after_sec: int = REAPER_STALE_SEC,
    queued_stale_sec: int = REAPER_QUEUED_STALE_SEC,
) -> None:
    logger.info(
        "reaper_started interval=%ds running_stale=%ds queued_stale=%ds",
        interval_sec,
        stale_after_sec,
        queued_stale_sec,
    )

    while True:
        try:
            await _reap_once(stale_after_sec, queued_stale_sec)
        except asyncio.CancelledError:
            logger.info("reaper_cancelled")
            raise
        except Exception:  # noqa: BLE001 — never let reaper die silently
            logger.exception("reaper_iter_error — continuing loop")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            logger.info("reaper_cancelled")
            raise
