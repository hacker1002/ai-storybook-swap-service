"""Demo handler `demo_long_running` — exercises the lib contract end-to-end.

Ported from `ai-storybook-python-api/src/jobs/handlers/demo_long_running.py`. No
AI, no real I/O — only `ctx.report` / `ctx.check_cancel` — so it validates the
enqueue → running CAS → report → terminal CAS + cooperative-cancel paths against
the asyncpg adapter without any upstream dependency.

Sequential-phases pattern (spec 00 §5.1):
    step_details = {phase_1: pending|running|completed|cancelled, ...}

Sleep is chunked into `CHUNK_SEC` slices so cancel latency ≤ CHUNK_SEC, even
when `step_interval_sec` is large (Validation Session 1 decision).
"""

from __future__ import annotations

import asyncio
import logging

from src.jobs.runner import JobContext, register

logger = logging.getLogger(__name__)

CHUNK_SEC = 5  # max cancel latency


@register("demo_long_running")
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    total = int(params.get("total_steps", 5))
    interval = int(params.get("step_interval_sec", 60))

    step_details: dict[str, str] = {
        f"phase_{i}": "pending" for i in range(1, total + 1)
    }
    errors: list[dict] = []

    async def _cancelled(i: int) -> tuple[str, dict]:
        step_details[f"phase_{i}"] = "cancelled"
        await ctx.report(current_step=i - 1, step_details=step_details)
        return (
            "cancelled",
            {
                "total_steps_done": i - 1,
                "errors": errors,
                "partial": True,
                "step_details": step_details,
            },
        )

    for i in range(1, total + 1):
        if await ctx.check_cancel():
            return await _cancelled(i)

        step_details[f"phase_{i}"] = "running"
        await ctx.report(current_step=i - 1, step_details=step_details)

        # Chunked sleep — cancel latency ≤ CHUNK_SEC.
        remaining = interval
        while remaining > 0:
            slice_sec = min(CHUNK_SEC, remaining)
            await asyncio.sleep(slice_sec)
            remaining -= slice_sec
            if await ctx.check_cancel():
                return await _cancelled(i)

        step_details[f"phase_{i}"] = "completed"
        await ctx.report(current_step=i, step_details=step_details)

    return (
        "completed",
        {
            "total_steps_done": total,
            "errors": errors,
            "step_details": step_details,
        },
    )
