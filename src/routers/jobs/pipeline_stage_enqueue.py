"""Shared enqueue flow for the crop-pipeline STAGE jobs (remix rmbg 09 · upscale 10)
— `POST /api/jobs/remix/{remix_id}/{rmbg|upscale}`.

Ported from `ai-storybook-image-api/src/routers/jobs/pipeline_stage_enqueue.py`
with the P3b seam swaps (README §7 delta):
  - `sb.table(...)` → `get_adapter()` (asyncpg AppDbAdapter). The image-api actor
    path (`table="actors"`) is out of scope here; the `table` arg stays for
    signature parity but the row load always goes through `get_remix` (this service
    has only the remixes table).
  - OWNERSHIP DELTA: the image-api snapshot→books.owner_id lookup is DROPPED
    (role-wide editor session — no per-user owner). Existence check only. `book_id`
    is resolved via `get_book_id_for_remix` and stamped on the job row;
    `enqueue(...)` forces `user_id` from settings internally.
  - Errors → `RemixDomainError` (rendered by the app-level handler as the spec
    envelope) instead of image-api's `error_response` HTTPException.
  - AUDIT: every enqueue writes `admin_ref`/`sid` into `params` + a structured
    `audit(...)` line (spec 00 §Audit — mandatory for role-wide sessions).

Steps (verbatim vs image-api):
  1. Load remix row (id, snapshot_id, <stage column>) → 404 `not_found_code`.
  2. book_id resolve (was: owner lookup; now: bridge lookup only, may be None).
  3. Resolve the batch by id within the stage column → 404 BATCH_NOT_FOUND.
  4. Collect sheets in scope (skip already-selected unless force_resweep).
  5. Skip responses (`no_crop_sheets` / `all_sheets_already_done`) — no row.
  6. Dedup PER-TYPE `(remix_id, job_type)` via `find_active_job` — the stage
     columns are disjoint JSONB, so sibling stage jobs run in parallel safely.
  7. Enqueue + init step_details + 201.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import find_batch_by_id

logger = logging.getLogger(__name__)

__all__ = ["collect_stage_scope", "enqueue_pipeline_stage_job"]


def collect_stage_scope(batch: dict, force_resweep: bool) -> tuple[list[int], bool]:
    """Compute in-scope flat sheet indices + has_sheets for a stage batch.

    - original_crops empty → not in scope (handler marks `skipped`).
    - force_resweep=false AND any swap_result is_selected → idempotent skip.
    (Ported verbatim from image-api.)
    """
    sheets = batch.get("crop_sheets") or []
    has_sheets = bool(sheets)
    in_scope: list[int] = []
    for i, sheet in enumerate(sheets):
        if not isinstance(sheet, dict) or not sheet.get("original_crops"):
            continue
        if not force_resweep and any(
            isinstance(r, dict) and r.get("is_selected")
            for r in (sheet.get("swap_results") or [])
        ):
            continue
        in_scope.append(i)
    return in_scope, has_sheets


async def enqueue_pipeline_stage_job(
    *,
    session: EditorSessionContext,
    table: str,
    row_id: str,
    id_key: str,
    not_found_code: str,
    batch_id: str,
    force_resweep: bool,
    stage_column: str,
    job_type: str,
    estimate_duration_sec: Callable[[dict, list[int]], int],
    model_params: dict | None = None,
    extra_params: dict | None = None,
) -> Any:
    """Run the shared stage-enqueue flow. Returns a response dict (200) or a
    201 JSONResponse; raises `RemixDomainError` on failures.

    `id_key` is the dedup filter key, the persisted `params[id_key]`, AND the
    response key. `estimate_duration_sec(batch, in_scope)` lets each stage supply
    its own heuristic. `model_params` (normalized) is merged into
    `params.model_params`; `extra_params` merged verbatim (e.g. job-10 `grain`).
    """
    del table  # signature parity — only the remixes table is served here.
    adapter = get_adapter()

    # 1. Load the owning remix row (full row → stage column + snapshot_id).
    try:
        row = await adapter.get_remix(row_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("row_load_failed row_id=%s type=%s", row_id, job_type)
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR", message="remix lookup failed"
        ) from exc

    if not row:
        raise RemixDomainError(
            status=404, code=not_found_code, message=f"remix {row_id} not found"
        )

    snapshot_id = row.get("snapshot_id")
    if not snapshot_id:
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR", message="remix row is missing snapshot_id"
        )

    # 2. book_id bridge (ownership DROPPED — existence check only). May be None.
    try:
        book_id = await adapter.get_book_id_for_remix(row_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("book_lookup_failed row_id=%s type=%s", row_id, job_type)
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR", message="book lookup failed"
        ) from exc

    # 3. Resolve the batch entry by id.
    batch = find_batch_by_id(row.get(stage_column) or [], batch_id)
    if not batch:
        raise RemixDomainError(
            status=404, code="BATCH_NOT_FOUND", message=f"batch {batch_id} not found"
        )

    # 4./5. Collect sheets in scope → skip responses.
    in_scope, has_sheets = collect_stage_scope(batch, force_resweep)
    if not has_sheets:
        return {
            "success": True,
            "data": {
                "skipped": True,
                "reason": "no_crop_sheets",
                "sheets_to_process": 0,
            },
        }
    if not in_scope:
        return {
            "success": True,
            "data": {
                "skipped": True,
                "reason": "all_sheets_already_done",
                "sheets_to_process": 0,
            },
        }

    # 6. Dedup — per-type `(remix_id, job_type)` (disjoint stage columns → safe).
    try:
        existing = await adapter.find_active_job(row_id, job_type)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dedup_check_failed row_id=%s type=%s", row_id, job_type)
        raise RemixDomainError(
            status=500, code="INTERNAL_ERROR", message="dedup lookup failed"
        ) from exc

    if existing:
        return {
            "success": True,
            "data": {
                "deduped": True,
                "job_id": str(existing["id"]),
                "status": existing["status"],
                "type": existing.get("type"),
                id_key: row_id,
                "active_key": (existing.get("params") or {}).get("batch_id"),
            },
        }

    # 7. Enqueue. `snapshot_id` recorded so the handler builds AiCallContext +
    #    resolves refs/geometry without re-querying; audit `admin_ref`/`sid`
    #    stamped into params (mandatory for role-wide sessions).
    job_params: dict = {
        id_key: row_id,
        "batch_id": batch_id,
        "force_resweep": force_resweep,
        "snapshot_id": snapshot_id,
        "admin_ref": session.admin_ref,
        "sid": session.sid,
    }
    if model_params is not None:
        job_params["model_params"] = model_params
    if extra_params:
        job_params.update(extra_params)
    try:
        job = await enqueue(
            type=job_type,
            params=job_params,
            book_id=book_id,
            total_steps=len(in_scope),
        )
    except ValueError as exc:
        raise RemixDomainError(
            status=500, code="JOB_INSERT_FAILED", message=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("enqueue_failed row_id=%s type=%s", row_id, job_type)
        raise RemixDomainError(
            status=500, code="JOB_INSERT_FAILED", message=str(exc)
        ) from exc

    # Init step_details (1 extra UPDATE; handlers have a defensive rebuild).
    step_details = {"sheets": {str(i): "pending" for i in in_scope}}
    try:
        await adapter.update_job(job["id"], {"step_details": step_details})
    except Exception as exc:  # noqa: BLE001
        logger.warning("step_details_init_failed job_id=%s msg=%s", job["id"], exc)

    sheets_to_process = len(in_scope)
    audit(
        session,
        endpoint=f"jobs.{job_type}",
        resource_id=row_id,
        job_id=str(job["id"]),
        batch_id=batch_id,
        sheets=sheets_to_process,
    )
    logger.info(
        "pipeline_stage_enqueued type=%s job_id=%s %s=%s sheets=%d force_resweep=%s",
        job_type, job["id"], id_key, row_id, sheets_to_process, force_resweep,
    )

    # 8. Return 201.
    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": job_type,
                id_key: row_id,
                "batch_id": batch_id,
                "total_steps": sheets_to_process,
                "sheets_to_process": sheets_to_process,
                "estimated_duration_sec": estimate_duration_sec(batch, in_scope),
            },
        },
    )
