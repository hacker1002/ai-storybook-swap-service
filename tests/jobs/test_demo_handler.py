"""End-to-end demo-handler test (no AI): enqueue → poll → cancel.

Importing `src.jobs.handlers` registers `demo_long_running` (side-effect). With
`step_interval_sec=0` the chunked sleep loop is skipped, so the whole lifecycle
runs synchronously-fast while still exercising the real report/cancel/terminal
paths against the fake adapter.
"""

from __future__ import annotations

from src.db.adapter import get_adapter
from src.jobs import enqueue, wait_all
from src.jobs.handlers import demo_long_running  # noqa: F401 — side-effect: @register("demo_long_running")


async def test_demo_runs_to_completed(fake_adapter):
    job = await enqueue(
        type="demo_long_running",
        params={"total_steps": 2, "step_interval_sec": 0},
        total_steps=2,
    )
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["status"] == "completed"
    assert row["current_step"] == 2
    assert row["result"]["total_steps_done"] == 2
    assert all(v == "completed" for v in row["result"]["step_details"].values())


async def test_demo_cancel_midflight_yields_cancelled(fake_adapter):
    job = await enqueue(
        type="demo_long_running",
        params={"total_steps": 3, "step_interval_sec": 0},
        total_steps=3,
    )
    await get_adapter().update_job(job["id"], {"cancel_requested": True})
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["status"] == "cancelled"
    assert row["result"]["partial"] is True
