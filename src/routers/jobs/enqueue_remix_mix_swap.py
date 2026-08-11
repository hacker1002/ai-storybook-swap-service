"""POST /api/jobs/remix/{remix_id}/mix-swap — enqueue mix-swap job.

Ported from image-api `src/routers/jobs/enqueue_remix_mix_swap.py`. Swaps the
WHOLE lineup of one mix entry (`remixes.mixes[]`) across every crop sheet via the
multi-target AI primitive `run_swap_mix_sheet`. The 201 success + 200 skipped
bodies are byte-identical to image-api.

⚠️ DEDUP DIVERGENCE (Phase-06 plan Insight 5 + Tiêu chí): mix-swap dedup returns
**409 JOB_ALREADY_ACTIVE** here (image-api returned 200 deduped). The plan groups
mix-swap with the 3 detect jobs (all 409). Dedup key = `remix_id`.

Service deltas vs image-api (same as sprite-swap): editor-session Bearer auth (no
X-API-Key), existence check instead of owner lookup, `admin_ref`/`sid` stamped
into params, DB via `get_adapter()`, `enqueue` without `user_id`.

Returns:
  - 201 + success data on enqueue.
  - 200 + skipped data when no sheet is in scope (no_crop_sheets /
    all_sheets_already_swapped) — no row created.
  - 409 JOB_ALREADY_ACTIVE when an active mix-swap job already exists for this remix.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_MIX_SWAP
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.jobs.model_registry import resolve_model_params
from src.models.jobs.remix_mix_swap import RemixMixSwapEnqueueRequest
from src.routers._shared.deps import error_response
from src.services.remix.mix_swap_resolver import (
    find_batch_by_id,
    resolve_mix_swap_context,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ESTIMATED_SEC_PER_SHEET = 42  # ⚡rev9 cut-only: swap (~40s) + cut + native
# uploads (~2s, Pillow only — no Replicate). Parity job 02 sprite-swap.


def _collect_mix_scope(batch: dict, force_resweep: bool) -> tuple[list[int], bool]:
    """Compute in-scope flat sheet indices + has_sheets.

    - original_crops empty → not in scope (handler marks `skipped`).
    - force_resweep=false AND any swap_result is_selected → idempotent skip.
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


@router.post("/remix/{remix_id}/mix-swap")
async def enqueue_remix_mix_swap_endpoint(
    remix_id: str,
    body: RemixMixSwapEnqueueRequest,
    ctx: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()
    batch_id = body.batch_id

    # 0. Resolve per-job model selection (group 'swap', SAME default as sprite —
    #    temp 0.25) — raises UNSUPPORTED_MODEL (422) early, BEFORE any DB work.
    model_params = resolve_model_params(
        body.model_params.model_dump() if body.model_params else None, "swap"
    )
    logger.info(
        "mix_swap_model_resolved remix_id=%s model=%s", remix_id, model_params["model"]
    )

    # 1. Load remix (existence check — NO per-user ownership).
    try:
        remix = await adapter.get_remix(UUID(remix_id))
    except ValueError as exc:
        raise error_response(404, "REMIX_NOT_FOUND", f"remix {remix_id} not found") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "remix lookup failed") from exc

    if not remix:
        raise error_response(404, "REMIX_NOT_FOUND", f"remix {remix_id} not found")

    remix_characters = remix.get("characters") or []
    remix_props = remix.get("props") or []
    remix_sprites = remix.get("sprites") or []
    mixes = remix.get("mixes") or []
    snapshot_id = remix.get("snapshot_id")
    if not snapshot_id:
        raise error_response(500, "INTERNAL_ERROR", "remix is missing snapshot_id")

    # 2. Resolve the batch entry by id.
    #    NOTE (Validation S1): NO INVALID_BATCH guard. The job swaps every batch
    #    with ≥1 resolvable swap_target (N=1 ≡ degenerate primitive 02). The only
    #    guard for a meaningless batch is NO_SWAP_TARGETS below.
    batch = find_batch_by_id(mixes, batch_id)
    if not batch:
        raise error_response(404, "BATCH_NOT_FOUND", f"batch {batch_id} not found")

    # 3. Resolve book_id + snapshot characters/props (no owner lookup).
    try:
        book_id = await adapter.get_book_id_for_remix(UUID(remix_id))
        snap = await adapter.get_current_snapshot(book_id, UUID(str(snapshot_id)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("snapshot_load_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "snapshot lookup failed") from exc

    snap_characters = (snap or {}).get("characters") or []
    snap_props = (snap or {}).get("props") or []

    mix_ctx = resolve_mix_swap_context(
        batch,
        remix_characters,
        remix_props,
        snap_characters,
        snap_props,
        remix_sprites=remix_sprites,
    )

    # Precondition fail-loud (lineup is constant → resolve once at enqueue).
    if mix_ctx.missing_char_refs:
        raise error_response(
            422,
            "MISSING_VARIANT_REFERENCE",
            "one or more character tokens are missing a sprite final reference",
            details={"tokens": list(dict.fromkeys(mix_ctx.missing_char_refs))},
        )
    if not mix_ctx.swap_targets:
        raise error_response(
            422, "NO_SWAP_TARGETS", "batch has no token resolvable to a swap target"
        )

    # 4. Collect sheets in scope.
    in_scope, has_sheets = _collect_mix_scope(batch, body.force_resweep)
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
                "reason": "all_sheets_already_swapped",
                "sheets_to_process": 0,
            },
        }

    # 5. Dedup — any active mix-swap job for this remix → 409 (Phase-06 plan
    #    divergence from image-api's 200; dedup key = remix_id).
    try:
        existing = await adapter.find_active_job(UUID(remix_id), JOB_TYPE_MIX_SWAP)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dedup_check_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "dedup lookup failed") from exc

    if existing:
        raise error_response(
            409,
            "JOB_ALREADY_ACTIVE",
            "a mix-swap job is already active for this remix",
            details={
                "job_id": str(existing["id"]),
                "status": existing["status"],
                "type": existing.get("type"),
                "remix_id": remix_id,
                "batch_id": (existing.get("params") or {}).get("batch_id"),
            },
        )

    # 6. Enqueue via jobs lib (user_id forced + params.source stamped inside).
    try:
        job = await enqueue(
            type=JOB_TYPE_MIX_SWAP,
            book_id=book_id,
            params={
                "remix_id": remix_id,
                "batch_id": batch_id,
                "force_resweep": body.force_resweep,
                "model_params": model_params,
                "admin_ref": ctx.admin_ref,
                "sid": ctx.sid,
            },
            total_steps=len(in_scope),
        )
    except ValueError as exc:
        raise error_response(500, "JOB_INSERT_FAILED", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("enqueue_failed remix_id=%s", remix_id)
        raise error_response(500, "JOB_INSERT_FAILED", str(exc)) from exc

    # 7. Init step_details (1 extra UPDATE; handler has a defensive rebuild).
    step_details = {"sheets": {str(i): "pending" for i in in_scope}}
    try:
        await adapter.update_job(job["id"], {"step_details": step_details})
    except Exception as exc:  # noqa: BLE001
        logger.warning("step_details_init_failed job_id=%s msg=%s", job["id"], exc)

    sheets_to_process = len(in_scope)

    audit(
        ctx,
        "POST /api/jobs/remix/{remix_id}/mix-swap",
        remix_id,
        job_id=str(job["id"]),
        type=JOB_TYPE_MIX_SWAP,
    )
    logger.info(
        "remix_mix_swap_enqueued job_id=%s remix_id=%s sheets=%d targets=%d force_resweep=%s",
        job["id"],
        remix_id,
        sheets_to_process,
        mix_ctx.target_count,
        body.force_resweep,
    )

    # 8. Return 201.
    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": JOB_TYPE_MIX_SWAP,
                "remix_id": remix_id,
                "batch_id": batch_id,
                "target_count": mix_ctx.target_count,
                "total_steps": sheets_to_process,
                "sheets_to_process": sheets_to_process,
                "estimated_duration_sec": sheets_to_process * _ESTIMATED_SEC_PER_SHEET,
            },
        },
    )
