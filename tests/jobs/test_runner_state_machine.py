"""Runner state-machine tests on the fake adapter (no DB, no AI).

Covers: enqueue → running CAS → report → terminal CAS; cooperative cancel →
`cancelled` (NOT `failed`); handler crash → `failed`; the queued→running CAS
admitting only ONE of two tasks racing the same job; enqueue attribution
(service `user_id` + `params.source`).

Each test registers a UNIQUELY-named handler, so `_REGISTRY` never collides
across tests — no registry-clearing fixture needed (done tasks self-discard from
`_TASKS` via the runner's `add_done_callback`).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from src.config.settings import settings
from src.db.adapter import get_adapter
from src.jobs import JobContext, enqueue, register, wait_all
from src.jobs import runner


async def test_enqueue_runs_to_completion(fake_adapter):
    @register("t_complete")
    async def _h(job: dict, ctx: JobContext):
        await ctx.report(current_step=1, step_details={"phase_1": "completed"})
        return ("completed", {"ok": True})

    job = await enqueue(type="t_complete", params={}, total_steps=1)
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["status"] == "completed"
    assert row["current_step"] == 1
    assert row["result"] == {"ok": True}
    assert row["step_details"] == {"phase_1": "completed"}


async def test_enqueue_stamps_service_user_id_and_source(fake_adapter):
    @register("t_attr")
    async def _h(job: dict, ctx: JobContext):
        return ("completed", None)

    job = await enqueue(type="t_attr", params={"remix_id": "abc"}, total_steps=1)
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    # user_id forced to the service identity (empty string in unit env), source stamped.
    assert row["user_id"] == settings.remix_swap_service_user_id
    assert row["params"]["source"] == "remix-swap-service"
    assert row["params"]["remix_id"] == "abc"  # caller params preserved


async def test_report_updates_progress(fake_adapter):
    @register("t_report")
    async def _h(job: dict, ctx: JobContext):
        await ctx.report(current_step=3, step_details={"a": "done"})
        return ("completed", None)

    job = await enqueue(type="t_report", params={}, total_steps=5)
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["current_step"] == 3
    assert row["step_details"] == {"a": "done"}


async def test_cooperative_cancel_yields_cancelled_not_failed(fake_adapter):
    @register("t_cancel")
    async def _h(job: dict, ctx: JobContext):
        for _ in range(50):
            if await ctx.check_cancel():
                return ("cancelled", {"partial": True})
            await asyncio.sleep(0)
        return ("completed", None)

    job = await enqueue(type="t_cancel", params={}, total_steps=1)
    # Flag cancel BEFORE the spawned task gets to run (no yield happened yet).
    await get_adapter().update_job(job["id"], {"cancel_requested": True})
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["status"] == "cancelled"
    assert row["result"] == {"partial": True}


async def test_handler_crash_marks_failed(fake_adapter):
    @register("t_crash")
    async def _h(job: dict, ctx: JobContext):
        raise RuntimeError("boom")

    job = await enqueue(type="t_crash", params={}, total_steps=1)
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["status"] == "failed"
    assert row["result"]["errors"][0]["stage"] == "internal"
    assert "boom" in row["result"]["errors"][0]["message"]


async def test_invalid_handler_status_marks_failed(fake_adapter):
    @register("t_badstatus")
    async def _h(job: dict, ctx: JobContext):
        return ("not_a_status", None)

    job = await enqueue(type="t_badstatus", params={}, total_steps=1)
    await wait_all()

    row = fake_adapter.jobs[str(job["id"])]
    assert row["status"] == "failed"


async def test_enqueue_unknown_type_raises(fake_adapter):
    with pytest.raises(ValueError):
        await enqueue(type="no_such_handler_type", params={}, total_steps=1)


async def test_concurrent_two_tasks_only_one_wins_cas(fake_adapter):
    calls: list = []

    @register("t_race")
    async def _h(job: dict, ctx: JobContext):
        calls.append(ctx.id)
        return ("completed", None)

    inserted = await get_adapter().insert_job(
        {"type": "t_race", "user_id": "", "params": {}, "status": "queued", "total_steps": 1}
    )
    # Two runner tasks on the SAME job row — the queued→running CAS must admit one.
    await asyncio.gather(
        runner._run(dict(inserted), time.monotonic()),
        runner._run(dict(inserted), time.monotonic()),
    )

    assert len(calls) == 1  # exactly one handler invocation
    assert fake_adapter.jobs[str(inserted["id"])]["status"] == "completed"


async def test_finalize_hook_fires_on_completion(fake_adapter):
    fired: list = []

    @register("t_finalhook")
    async def _h(job: dict, ctx: JobContext):
        return ("completed", {"n": 1})

    from src.jobs import register_finalize

    @register_finalize("t_finalhook")
    async def _f(job: dict, status: str, result):
        fired.append((status, result))

    job = await enqueue(type="t_finalhook", params={}, total_steps=1)
    await wait_all()

    assert fired == [("completed", {"n": 1})]
    assert fake_adapter.jobs[str(job["id"])]["status"] == "completed"


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot + restore the runner registries so uniquely-named handlers from
    one test never accumulate into another's view (and finalize hooks don't leak).
    """
    reg = dict(runner._REGISTRY)
    fin = dict(runner._FINALIZE_HOOKS)
    yield
    runner._REGISTRY.clear()
    runner._REGISTRY.update(reg)
    runner._FINALIZE_HOOKS.clear()
    runner._FINALIZE_HOOKS.update(fin)
