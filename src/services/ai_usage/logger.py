"""`AiLogEntry` + the ONE fire-and-forget insert path into `ai_service_logs`.

Ported from `ai-storybook-image-api/src/services/ai_usage/logger.py`. Only the DB
write path changed — everything else (sanitize-at-choke, cost dict shape, ref/output
blob attach) is preserved. Divergences forced by this service's seams:

  - WRITE: `await get_adapter().insert_ai_log(row)` (asyncpg) replaces
    `supabase.table("ai_service_logs").insert(...)`. Because that call is async, the
    fire-and-forget mechanism schedules an `asyncio.Task` (strong-ref'd in
    `_LOG_TASKS`) instead of a thread-executor job. `drain()` awaits the pending
    tasks at shutdown so the last rows land.
  - ATTRIBUTION: `user_id` is ALWAYS NULL; `admin_ref`/`sid` ride nested under
    `request.audit` together with `source="remix-swap-service"` (cost discriminator
    vs the editor app writing the same table). `book_id` is resolved from `remix_id`
    via the `remixes.snapshot_id → snapshots.book_id` bridge (`get_book_id_for_remix`)
    and CACHED on the ctx so repeated AI calls don't re-query.
  - `id` is CLIENT-MINTED (parity restored 260812, đảo divergence P3b): choke points
    mint `new_request_id()` BEFORE the provider call and pass it as `entry.id`, so the
    row `id` == the `ai_request_id` surfaced in response envelopes (image-api
    semantics — enables future provenance lookup by envelope id). The logger mints a
    fallback uuid4 when a caller omits it; the DB `gen_random_uuid()` default is now
    the last-resort only.

Invariants (unchanged): logging NEVER fails the main request — every exception on
the insert path is swallowed + `log.warning`, no retry, no queue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from src.db.adapter import get_adapter
from src.services.ai_usage.context import AiCallContext
from src.services.ai_usage.sanitize import OUTPUT_PREFIX, build_ref_metadata

logger = logging.getLogger(__name__)

# Marker stashed in `request.audit.source` so this service's rows are separable from
# the editor app's rows in the shared `ai_service_logs` table (cost rollup).
AUDIT_SOURCE = "remix-swap-service"

# uuid columns on ai_service_logs — coerced from the ctx's `str` ids back to
# `uuid.UUID` for asyncpg at the DB boundary (mirrors the codebase convention of
# passing UUID objects, e.g. create_remix). user_id is intentionally excluded (NULL).
_UUID_COLUMNS = ("book_id", "snapshot_id", "remix_id", "job_id")

# Strong-ref the fire-and-forget insert tasks (mirror image-api's `_LOG_TASKS`) so
# the event loop does not garbage-collect a pending insert mid-flight.
_LOG_TASKS: set[asyncio.Task] = set()


@dataclass(frozen=True)
class AiLogEntry:
    """Internal payload between a choke point and the logger.

    `request`/`response` are ALREADY sanitized by the choke point
    (`sanitize_request` / `sanitize_response`). `cost` is the `compute_cost(...)`
    return dict (`{costUsd, costSource, pricingVersion}`), or None. `ref_blobs` /
    `output_blobs` are optional file refs recorded (by content hash) into
    `request.ref_files` / `response.output_files`.
    """

    provider: str  # 'gemini' | 'replicate' | 'elevenlabs'
    operation: str  # run_name of the call
    model: str | None
    status: str  # 'success' | 'error'
    context: AiCallContext
    request: dict
    # Row id == the pre-call `new_request_id()` the choke point surfaced as
    # `ai_request_id` in its envelope. None/malformed → logger mints a uuid4 so the
    # insert never fails on it.
    id: str | None = None
    response: dict | None = None
    error: str | None = None
    latency_ms: int | None = None
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_unit: str | None = None  # 'tokens' | 'seconds' | 'characters'
    usage_amount: float | None = None
    cost: dict | None = None  # compute_cost() → {costUsd, costSource, pricingVersion}
    ref_blobs: tuple = ()  # optional ((bytes|url, mime), ...) INPUT files → request.ref_files
    output_blobs: tuple = ()  # optional RAW OUTPUT files → response.output_files


def new_request_id() -> str:
    """A client-side uuid4 string, minted BEFORE the provider call. Doubles as the
    `ai_service_logs.id` row id (pass it as `AiLogEntry.id`) AND the `ai_request_id`
    correlation id in the caller's response envelope — image-api parity (restored
    260812), so an envelope id is always resolvable via `get_ai_log`."""
    return str(uuid4())


def _to_uuid(value) -> UUID | None:
    """Best-effort `str → uuid.UUID` for a uuid column. `None`/blank/malformed → None
    (never raises — the log path must not fail on a stray id)."""
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _entry_to_row(entry: AiLogEntry) -> dict:
    """Map `AiLogEntry` → `ai_service_logs` columns (allowlisted only).

    NOT-NULL columns (`provider`/`operation`/`model`/`status`/`request`) are coerced
    so a partial entry can never make the insert fail on a constraint (which would
    drop the row). `user_id` is forced NULL; `admin_ref`/`sid`/`source` are nested in
    `request.audit`."""
    ctx = entry.context or AiCallContext()
    request = dict(entry.request) if isinstance(entry.request, dict) else {"value": entry.request}

    # Nested audit block (NOT flattened into AI params → keeps analysis clean).
    request["audit"] = {
        "admin_ref": ctx.admin_ref,
        "sid": ctx.sid,
        "source": AUDIT_SOURCE,
    }

    if entry.ref_blobs:
        refs = []
        for blob in entry.ref_blobs:
            data, mime = blob if isinstance(blob, (tuple, list)) else (blob, None)
            refs.append(build_ref_metadata(data, mime=mime))
        request["ref_files"] = refs

    # Raw AI output → `response.output_files[]`.
    response = entry.response
    if entry.output_blobs:
        files = []
        for blob in entry.output_blobs:
            if isinstance(blob, dict):
                files.append(blob)
            else:
                data, mime = blob if isinstance(blob, (tuple, list)) else (blob, None)
                files.append(build_ref_metadata(data, mime=mime, prefix=OUTPUT_PREFIX))
        if isinstance(response, dict):
            response = dict(response)
        elif response is None:
            response = {}
        else:
            response = {"value": response}
        response["output_files"] = files

    row = {
        # Client-minted id (== envelope ai_request_id); fallback mint keeps the
        # insert alive when a caller omitted/mangled it.
        "id": _to_uuid(entry.id) or uuid4(),
        "provider": entry.provider,
        "operation": entry.operation,
        "model": entry.model or "unknown",
        "status": entry.status or "success",
        "error": entry.error,
        "latency_ms": entry.latency_ms,
        # attribution — user_id ALWAYS NULL; uuid columns coerced str → UUID.
        "book_id": _to_uuid(ctx.book_id),
        "snapshot_id": _to_uuid(ctx.snapshot_id),
        "remix_id": _to_uuid(ctx.remix_id),
        "job_id": _to_uuid(ctx.job_id),
        "user_id": None,
        "request": request,
        "response": response,
        "provider_request_id": entry.provider_request_id,
        "input_tokens": entry.input_tokens,
        "output_tokens": entry.output_tokens,
        "total_tokens": entry.total_tokens,
        "usage_unit": entry.usage_unit,
        "usage_amount": entry.usage_amount,
    }
    if entry.cost:
        row["cost_usd"] = entry.cost.get("costUsd")
        row["cost_source"] = entry.cost.get("costSource")
        row["pricing_version"] = entry.cost.get("pricingVersion")
    return row


async def _resolve_book_id(ctx: AiCallContext, row: dict) -> None:
    """Fill `row['book_id']` from the `remix_id → book_id` bridge when the ctx carried
    no explicit book_id. Result is CACHED on `ctx._book_cache` so N AI calls sharing
    one ctx trigger at most ONE bridge query. Any failure is swallowed + warned —
    book_id stays None, the insert still proceeds (never raises)."""
    if row.get("book_id") is not None or not ctx.remix_id:
        return
    cache = ctx._book_cache
    if "book_id" in cache:
        row["book_id"] = cache["book_id"]
        return
    remix_uuid = _to_uuid(ctx.remix_id)
    resolved: UUID | None = None
    if remix_uuid is not None:
        try:
            resolved = await get_adapter().get_book_id_for_remix(remix_uuid)
        except Exception as e:  # noqa: BLE001 — resolve never fails the log write
            logger.warning(
                "ai_usage_book_id_resolve_failed remix_id=%s reason=%s",
                ctx.remix_id, type(e).__name__,
            )
    resolved = _to_uuid(resolved)
    cache["book_id"] = resolved
    row["book_id"] = resolved


async def _insert(entry: AiLogEntry) -> None:
    """Async insert of one row via the DB adapter. Swallows EVERY error → the log
    path can never surface an exception to the caller (runs as a background task)."""
    try:
        row = _entry_to_row(entry)
        await _resolve_book_id(entry.context or AiCallContext(), row)
        await get_adapter().insert_ai_log(row)
    except Exception as e:  # noqa: BLE001 — fire-and-forget: never propagate
        logger.warning(
            "ai_usage_insert_failed provider=%s op=%s reason=%s",
            entry.provider, entry.operation, e,
        )


def log_ai_request(entry: AiLogEntry) -> None:
    """Schedule the insert as a strong-referenced background task. Never blocks,
    never raises. Off the event loop (edge case) it swallows + warns (no insert)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "ai_usage_log_no_loop provider=%s op=%s (row dropped)",
            entry.provider, entry.operation,
        )
        return
    task = loop.create_task(_insert(entry))
    _LOG_TASKS.add(task)
    task.add_done_callback(_LOG_TASKS.discard)


async def drain(timeout: float | None = None) -> None:
    """Await all pending log inserts — call in lifespan shutdown so the last rows are
    written before the pool closes. Never raises (each task swallows its own errors);
    with `timeout`, returns after that many seconds even if some inserts still hang."""
    if not _LOG_TASKS:
        return
    pending = list(_LOG_TASKS)
    if timeout is not None:
        await asyncio.wait(pending, timeout=timeout)
    else:
        await asyncio.gather(*pending, return_exceptions=True)
