"""Keep-alive heartbeat for long single-`await` worker calls.

Ported from `ai-storybook-python-api/src/jobs/helpers/heartbeat.py`. Pure — no DB
seam of its own; it drives `ctx.report(...)` (which goes through the adapter).
Only the settings import differs (this service's `settings.job_heartbeat_sec`).

Problem: a handler that makes ONE long `await` (30–90s Gemini/Replicate) with no
intermediate `ctx.report(...)` leaves `background_jobs.updated_at` frozen for the
whole call. The reaper (`REAPER_STALE_SEC`) sweeps `running` rows whose
`updated_at` is stale and finalizes them `failed`; a long call that outlives the
threshold would be reclaimed mid-flight, then the later success would write a
`failed` leaf (finalize-before-status) — breaking the race-fix for long jobs.

Fix: wrap the long `await` in `heartbeat(...)`; a background task bumps
`updated_at` (via `ctx.report`) every `interval_sec` so the row never looks
stale. The beat stops the instant the wrapped block exits (success OR raise).

Invariants:
  - NEVER lets a `ctx.report` failure (DB blip) kill the worker call — logs a
    warning and keeps beating.
  - Cancellation-safe: the wrapped block raising `CancelledError` (lifespan
    abort) tears the beat task down cleanly in `finally` — no leaked task.
  - `interval_sec` MUST keep margin ≥3× under the reaper stale threshold
    (`settings.job_heartbeat_sec`=30 ≪ `REAPER_STALE_SEC`=1800).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Callable

from src.config.settings import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def heartbeat(
    ctx,
    *,
    step_details_fn: Callable[[], dict],
    current_step_fn: Callable[[], int] = lambda: 0,
    interval_sec: int | None = None,
):
    """Bump `ctx`'s `updated_at` every `interval_sec` for the duration of the
    wrapped `async with` block.

    Args:
        ctx: a `JobContext` (uses `ctx.report(current_step=, step_details=)`).
        step_details_fn: called each beat to snapshot the current `step_details`
            (a callable, not a value, so the beat reflects live handler state).
        current_step_fn: called each beat for the `current_step` value.
        interval_sec: beat period; defaults to `settings.job_heartbeat_sec`.

    The beat task is created on enter and awaited-to-completion on exit, so no
    task leaks past the context. A `ctx.report` error is swallowed (logged) — a
    transient DB error must not abort a minutes-long worker call.
    """
    period = interval_sec if interval_sec is not None else settings.job_heartbeat_sec
    stop = asyncio.Event()

    async def _beat() -> None:
        while True:
            try:
                # Wake either when stopped (block exited) or after `period`.
                await asyncio.wait_for(stop.wait(), timeout=period)
                return  # stop set → exit cleanly
            except asyncio.TimeoutError:
                try:
                    await ctx.report(
                        current_step=current_step_fn(),
                        step_details=step_details_fn(),
                    )
                except Exception:  # noqa: BLE001 — heartbeat must never break the worker call
                    logger.warning("job_heartbeat_report_failed id=%s", getattr(ctx, "id", "?"))

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        stop.set()
        # Await the beat task so it never outlives the context (no leak). `stop.set()`
        # makes `_beat` return promptly UNLESS a `ctx.report` is in-flight at this
        # instant — and `ctx.report` could in theory hang on a stuck DB call. Bound
        # the teardown so a hung report never blocks the wrapped worker call's exit
        # indefinitely (M1): wait up to one interval + small slack, then cancel the
        # beat task and move on.
        try:
            await asyncio.wait_for(task, timeout=period + 5)
        except asyncio.TimeoutError:
            # wait_for already cancelled the task on timeout; ensure it is fully
            # settled, swallowing the resulting CancelledError (teardown cleanup).
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown cleanup
                pass
        except asyncio.CancelledError:
            # Propagated cancellation (lifespan abort during teardown await) —
            # ensure the beat task is fully done, then re-raise to the caller.
            if not task.done():
                task.cancel()
            raise
