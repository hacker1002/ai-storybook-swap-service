"""Reaper sweep tests on the fake adapter.

Drives `_reap_once` directly (deterministic; no need to run the infinite
`reaper_loop`). Covers: stale-running → failed('stale worker'); stale-queued →
failed('spawn race'); fresh rows untouched; finalize hook fires exactly once per
swept row and a second sweep is a no-op (idempotent + CAS-guarded).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from src.jobs import reaper
from src.jobs import runner


def _job(
    status: str,
    *,
    updated_delta_s: int = 0,
    created_delta_s: int = 0,
    job_type: str = "demo",
    source: str = "remix-swap-service",
) -> dict:
    now = datetime.now(tz=timezone.utc)
    jid = uuid.uuid4()
    return {
        "id": jid,
        "type": job_type,
        "status": status,
        # `source` mirrors what enqueue stamps; the reaper sweep is scoped to it.
        "params": {"source": source} if source else {},
        "updated_at": now - timedelta(seconds=updated_delta_s),
        "created_at": now - timedelta(seconds=created_delta_s),
    }


async def test_reaper_sweeps_stale_running(fake_adapter):
    row = _job("running", updated_delta_s=2000)  # > REAPER_STALE_SEC (1800)
    fake_adapter.jobs[str(row["id"])] = row

    swept_running, swept_queued = await reaper._reap_once(1800, 300)

    assert (swept_running, swept_queued) == (1, 0)
    saved = fake_adapter.jobs[str(row["id"])]
    assert saved["status"] == "failed"
    assert saved["result"]["errors"][0]["message"] == "stale worker"


async def test_reaper_sweeps_stale_queued(fake_adapter):
    row = _job("queued", created_delta_s=400)  # > REAPER_QUEUED_STALE_SEC (300)
    fake_adapter.jobs[str(row["id"])] = row

    swept_running, swept_queued = await reaper._reap_once(1800, 300)

    assert (swept_running, swept_queued) == (0, 1)
    saved = fake_adapter.jobs[str(row["id"])]
    assert saved["status"] == "failed"
    assert saved["result"]["errors"][0]["message"] == "spawn race"


async def test_reaper_leaves_fresh_rows(fake_adapter):
    running_fresh = _job("running", updated_delta_s=10)
    queued_fresh = _job("queued", created_delta_s=10)
    fake_adapter.jobs[str(running_fresh["id"])] = running_fresh
    fake_adapter.jobs[str(queued_fresh["id"])] = queued_fresh

    swept = await reaper._reap_once(1800, 300)

    assert swept == (0, 0)
    assert fake_adapter.jobs[str(running_fresh["id"])]["status"] == "running"
    assert fake_adapter.jobs[str(queued_fresh["id"])]["status"] == "queued"


async def test_reaper_ignores_foreign_source_jobs(fake_adapter):
    """H2 guard: a stale job authored by another service (e.g. image-api
    export_pdf) on the SHARED background_jobs table must NOT be swept — this
    service has no finalize hook for it, so flipping it would orphan the leaf."""
    foreign = _job("running", updated_delta_s=5000, job_type="export_pdf", source="image-api")
    fake_adapter.jobs[str(foreign["id"])] = foreign

    swept = await reaper._reap_once(1800, 300)

    assert swept == (0, 0)
    assert fake_adapter.jobs[str(foreign["id"])]["status"] == "running"  # untouched


async def test_reaper_finalize_fires_once_and_second_sweep_noop(fake_adapter):
    fired: list = []

    @runner.register_finalize("reap_final_type")
    async def _f(job: dict, status: str, result):
        fired.append(status)

    try:
        row = _job("running", updated_delta_s=2000, job_type="reap_final_type")
        fake_adapter.jobs[str(row["id"])] = row

        first = await reaper._reap_once(1800, 300)
        second = await reaper._reap_once(1800, 300)  # row now 'failed' → not stale, CAS-guarded

        assert first == (1, 0)
        assert second == (0, 0)
        assert fired == ["failed"]  # exactly once
    finally:
        runner._FINALIZE_HOOKS.pop("reap_final_type", None)
