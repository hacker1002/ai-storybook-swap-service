"""Job runner — registry, enqueue, JobContext, internal lifecycle.

Ported from `ai-storybook-python-api/src/jobs/runner.py`. THE ONLY change is the
I/O seam: every `sb.table("background_jobs")...` Supabase call becomes a
`get_adapter().*` asyncpg call. The state machine, the CAS ordering, the
finalize-before-status ordering, the per-type semaphore, and the strong-ref
`_TASKS` leak guard are preserved VERBATIM — do NOT rewrite them.

State machine (spec 00 §6.1):
    queued → running → completed | failed | cancelled

CAS translation (image-api → this service), kept exact:
  - `.update({...}).eq("id",id).eq("status","running").execute()` truthy `resp.data`
    → `adapter.update_job(id, {...}, expect_status="running")` returning bool
    (True == rowcount 1 == won the CAS).
  - `.update({... "updated_at": "now()"})` → the adapter stamps `updated_at = now()`
    on every UPDATE, so the runner never carries an explicit `now()` field.

Attribution (service delta): every enqueued row uses
`settings.remix_swap_service_user_id` (NOT NULL FK) and stamps
`params.source = "remix-swap-service"` so this service's rows are separable from
the editor app's in the shared `background_jobs` table.

Connection discipline (README §7): the adapter acquires a connection per
statement and releases it — the runner NEVER holds one across a handler `await`
(the AI calls in P3b handlers run BETWEEN adapter calls, not inside one).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Awaitable, Callable

from src.config.settings import settings
from src.core.job_types import SERVICE_SOURCE
from src.db.adapter import get_adapter
from src.jobs.config import get_sem_cap

logger = logging.getLogger(__name__)

HandlerResult = tuple[str, dict | None]
Handler = Callable[[dict, "JobContext"], Awaitable[HandlerResult]]
# Finalize hook: (job_row, terminal_status, result_payload) → None. Invoked
# AFTER the row reaches a terminal status (runner reap-guard OK, or reaper sweep)
# so handlers can materialize side state (e.g. a distribution leaf) on EVERY
# terminal path, decoupled from the generic runner.
FinalizeHook = Callable[[dict, str, "dict | None"], Awaitable[None]]

_REGISTRY: dict[str, Handler] = {}
_FINALIZE_HOOKS: dict[str, FinalizeHook] = {}
_TASKS: set[asyncio.Task] = set()
_SEMS: dict[str, asyncio.Semaphore] = {}

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


# ─── public API ──────────────────────────────────────────────────────────────


def register(job_type: str) -> Callable[[Handler], Handler]:
    """Decorator: register an async handler for `job_type`.

    Raises:
        TypeError: handler is not `async def`.
        RuntimeError: `job_type` already has a handler (idempotent guard).
    """

    def decorator(fn: Handler) -> Handler:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"handler for {job_type!r} must be `async def`")
        if job_type in _REGISTRY:
            raise RuntimeError(f"handler already registered for {job_type!r}")
        _REGISTRY[job_type] = fn
        logger.info("job_handler_registered type=%s", job_type)
        return fn

    return decorator


def register_finalize(job_type: str) -> Callable[[FinalizeHook], FinalizeHook]:
    """Decorator: register a finalize hook for `job_type` (mirrors `register`).

    The hook fires once per terminal transition, only when this task/reaper
    actually finalized the row (reap-guard returned data) — so it never
    double-fires for the same job.

    Raises:
        TypeError: hook is not `async def`.
        RuntimeError: `job_type` already has a finalize hook.
    """

    def decorator(fn: FinalizeHook) -> FinalizeHook:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"finalize hook for {job_type!r} must be `async def`")
        if job_type in _FINALIZE_HOOKS:
            raise RuntimeError(f"finalize hook already registered for {job_type!r}")
        _FINALIZE_HOOKS[job_type] = fn
        logger.info("job_finalize_hook_registered type=%s", job_type)
        return fn

    return decorator


async def _run_finalize(job: dict, status: str, result: dict | None) -> None:
    """Look up + invoke the finalize hook for `job`'s type. Swallows any error
    (a side-effect hook must never break the runner/reaper)."""
    hook = _FINALIZE_HOOKS.get(job.get("type"))
    if hook is None:
        return
    try:
        await hook(job, status, result)
    except Exception:  # noqa: BLE001 — hook failure must not propagate
        logger.exception(
            "job_finalize_hook_error id=%s type=%s status=%s",
            job.get("id"), job.get("type"), status,
        )


async def _finalize_then_commit(job: dict, status: str, result_payload: dict | None) -> bool:
    """Materialize side state (finalize hook) FIRST, then CAS-flip the job to a
    terminal `status` — only if the row is still `running`. Returns True if the
    status commit won, False if the reaper already reclaimed the row.

    ⚠️ DO NOT "optimize" the two steps (finalize hook, then status CAS) into a
    single DB transaction. The ordering is DELIBERATE and load-bearing — keep it
    verbatim:

    Ordering rationale (finalize-before-status, plan
    `260607-1453-distribution-export-realtime-race-fix` phase-01): the realtime
    consumer only subscribes to `background_jobs.status` (the side-state column
    has NO realtime). So the terminal `status` event MUST NOT fire before the
    side state is materialized — otherwise the FE refetch triggered by that event
    reads a half-written snapshot (Bug 2). Doing finalize first guarantees every
    leaf is at its terminal state before any consumer is told the job ended. This
    service polls instead of using realtime, but the ordering is KEPT to prevent
    drift with image-api + preserve the same crash/reaper-safety invariants.

    Safe to keep two-step because every finalize hook is CAS-guarded on
    `leaf.job_id == job_id` → idempotent + reaper-safe regardless of order:
      - Reaper reclaims BEFORE the worker finalize → reaper writes leaf 'failed';
        the worker's later finalize CAS-skips; this commit's CAS returns 0 rows →
        False. Net: job=failed, leaf=failed. Consistent.
      - Reaper reclaims in the sub-ms gap AFTER the worker finalize but BEFORE
        this commit → worker leaf stands (reaper finalize CAS-skips), commit
        returns 0 rows → False. Net: job=failed, leaf=updated(real artifact).
        Inconsistent but BENIGN (the artifact exists). Accepted + documented.
    """
    await _run_finalize(job, status, result_payload)
    return await get_adapter().update_job(
        job["id"],
        {"status": status, "result": result_payload},
        expect_status="running",
    )


async def enqueue(
    *,
    type: str,
    params: dict,
    book_id: str | None = None,
    total_steps: int = 1,
) -> dict:
    """Insert a queued row + spawn the runner task. Returns the inserted row.

    Caller does not await job completion — fire-and-forget. Strong-ref the
    task in `_TASKS` so asyncio does not GC it mid-flight.

    Service deltas vs image-api:
      - `user_id` is ALWAYS `settings.remix_swap_service_user_id` (this service
        has no user directory; the FK column is NOT NULL). Real attribution lives
        in `params`.
      - `params.source = "remix-swap-service"` is stamped so cost/audit rollups
        can separate this service's rows from the editor app's.
    """
    if type not in _REGISTRY:
        raise ValueError(f"no handler registered for type={type!r}")

    stamped_params = {**(params or {}), "source": SERVICE_SOURCE}
    row = {
        "type": type,
        "user_id": settings.remix_swap_service_user_id,
        "book_id": book_id,
        "params": stamped_params,
        "total_steps": total_steps,
        "step_details": {},
        "status": "queued",
    }
    job = await get_adapter().insert_job(row)
    if not job:
        raise RuntimeError(f"insert returned empty data for type={type!r}")

    enqueued_at = time.monotonic()
    task = asyncio.create_task(_run(job, enqueued_at), name=f"job:{type}:{job['id']}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    logger.info(
        "job_enqueued id=%s type=%s total_steps=%d",
        job["id"],
        type,
        total_steps,
    )
    return job


class JobContext:
    """Handler-facing API for progress reporting + cancellation polling.

    Every method is ONE short adapter call (acquire-per-statement) — safe to call
    at step boundaries between long AI awaits without holding a connection.
    """

    id: object
    total_steps: int

    def __init__(self, id, total_steps: int) -> None:
        self.id = id
        self.total_steps = total_steps

    async def report(self, *, current_step: int, step_details: dict) -> None:
        """UPDATE `current_step` + `step_details` (adapter bumps `updated_at`).

        Unconditional (no `expect_status`) — progress reports may land while the
        row is `running`; a reaper-reclaimed row simply no-ops the UPDATE later
        at the terminal CAS.
        """
        await get_adapter().update_job(
            self.id,
            {"current_step": current_step, "step_details": step_details},
        )

    async def check_cancel(self) -> bool:
        """SELECT the live `cancel_requested` flag — handler MUST call at every
        step boundary. A missing poll = an uncancellable job."""
        job = await get_adapter().get_job(self.id)
        return bool(job and job.get("cancel_requested"))


# ─── internal ────────────────────────────────────────────────────────────────


def _get_sem(job_type: str) -> asyncio.Semaphore:
    """Lazy per-type semaphore. Asyncio is single-threaded → no lock needed."""
    sem = _SEMS.get(job_type)
    if sem is None:
        cap = get_sem_cap(job_type)
        sem = asyncio.Semaphore(cap)
        _SEMS[job_type] = sem
        logger.debug("job_sem_created type=%s cap=%d", job_type, cap)
    return sem


async def _run(job: dict, enqueued_at: float) -> None:
    """Wrap `_execute` in the per-type concurrency semaphore.

    Emits `job_sem_acquired` on acquisition — the observability gap for the
    queued→running transition (no DB row reflects time spent blocked):
      - `sem_wait_ms`   — time blocked on the per-type semaphore (>0 ⇒ cap saturated).
      - `queue_wait_ms` — total latency since enqueue; minus `sem_wait_ms` it
        approximates event-loop scheduling lag (large ⇒ loop CPU-starved).
    """
    job_type = job["type"]
    sem = _get_sem(job_type)
    wait_start = time.monotonic()
    async with sem:
        acquired_at = time.monotonic()
        logger.info(
            "job_sem_acquired id=%s type=%s sem_wait_ms=%.1f queue_wait_ms=%.1f sem_cap=%d",
            job["id"],
            job_type,
            (acquired_at - wait_start) * 1000,
            (acquired_at - enqueued_at) * 1000,
            get_sem_cap(job_type),
        )
        await _execute(job)


async def _execute(job: dict) -> None:
    """State transitions: queued → running → completed | failed | cancelled."""
    job_id = job["id"]
    job_type = job["type"]
    handler = _REGISTRY[job_type]

    started = await get_adapter().update_job(
        job_id, {"status": "running"}, expect_status="queued"
    )
    if not started:
        # Reaper reclaimed the row (queued > REAPER_QUEUED_STALE_SEC while this
        # task waited on the per-type semaphore) OR a sibling task won the CAS.
        # Abort — running the handler would resurrect a row the reaper already
        # finalized as `failed`, or double-run a job.
        logger.warning(
            "job_start_aborted id=%s type=%s — row no longer 'queued' "
            "(reaper reclaimed during semaphore wait, or lost CAS to a sibling); "
            "skipping handler",
            job_id,
            job_type,
        )
        return
    logger.info("job_running id=%s type=%s", job_id, job_type)

    ctx = JobContext(id=job_id, total_steps=job["total_steps"])
    try:
        result = await handler(job, ctx)
        if not (isinstance(result, tuple) and len(result) == 2):
            raise ValueError(
                f"handler must return (status, result_dict) tuple, got {type(result).__name__}"
            )
        status, result_payload = result
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"handler returned invalid status: {status!r}")

        # Finalize-before-status (phase-01): materialize side state FIRST, then
        # CAS-flip status. `_finalize_then_commit` returns False when the reaper
        # already reclaimed the row — preserve the reaper's `failed` verdict.
        committed = await _finalize_then_commit(job, status, result_payload)
        if not committed:
            logger.warning(
                "job_status_commit_lost_to_reaper id=%s type=%s intended_status=%s "
                "— row no longer 'running'; finalize already ran (leaf-CAS safe)",
                job_id,
                job_type,
                status,
            )
            return
        logger.info("job_done id=%s type=%s status=%s", job_id, job_type, status)

    except asyncio.CancelledError:
        # Lifespan-initiated abort: finalize (release leaves) FIRST, then CAS-flip
        # cancelled. Re-raise unconditionally so the task done-callback cleans up
        # _TASKS, whether or not the reaper beat us to the status commit.
        committed = await _finalize_then_commit(job, "cancelled", None)
        if not committed:
            logger.warning(
                "job_cancel_status_commit_lost_to_reaper id=%s type=%s "
                "— finalize already ran (leaf-CAS safe)",
                job_id,
                job_type,
            )
        else:
            logger.warning("job_aborted_by_lifespan id=%s type=%s", job_id, job_type)
        raise

    except Exception as exc:  # noqa: BLE001 — top-level handler safety net
        logger.exception("job_crashed id=%s type=%s", job_id, job_type)
        fail_result = {
            "errors": [
                {"stage": "internal", "message": f"{type(exc).__name__}: {exc}"}
            ]
        }
        committed = await _finalize_then_commit(job, "failed", fail_result)
        if not committed:
            logger.warning(
                "job_crash_status_commit_lost_to_reaper id=%s type=%s "
                "— row no longer 'running'; reaper already finalized (leaf-CAS safe)",
                job_id,
                job_type,
            )
