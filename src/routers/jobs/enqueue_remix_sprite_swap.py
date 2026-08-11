"""POST /api/jobs/remix/{remix_id}/sprite-swap — enqueue sprite-swap job.

Ported from image-api `src/routers/jobs/enqueue_remix_sprite_swap.py`. Swaps every
in-scope crop sheet of ONE sprite entry (`remixes.sprites[]`) via the per-object
per-trait AI primitive. Response bodies (201 success / 200 skipped / 200 dedup)
are byte-identical to image-api so the FE `jobs-api.ts` parser is unchanged.

Service deltas vs image-api:
  - Auth: editor-session Bearer (router-group `require_editor_session`), NOT
    X-API-Key. NO per-user ownership — an existence check (404 REMIX_NOT_FOUND)
    replaces the owner lookup; `admin_ref`/`sid` from the session are stamped into
    `params` (audit) + a structured audit line.
  - DB via `get_adapter()` (asyncpg): `get_remix`, `get_current_snapshot`,
    `list_humans` (global load, filtered), `get_book_id_for_remix`,
    `find_active_job`, `update_job` (step_details init).
  - `enqueue(type, params, book_id, total_steps)` — `user_id` is forced inside the
    lib; `params.source` is stamped inside the lib.

Returns:
  - 201 + success data on enqueue.
  - 200 + skipped data when no sheet is in scope (no_crop_sheets /
    all_sheets_already_swapped) — no row created.
  - 200 + dedup data when an active sprite-swap job already exists for this remix.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_SPRITE_SWAP
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.jobs.model_registry import resolve_model_params
from src.models.jobs.remix_sprite_swap import RemixSpriteSwapEnqueueRequest
from src.routers._shared.deps import error_response
from src.services.remix.sprite_swap_resolver import (
    find_sprite_by_id,
    resolve_sprite_object_map,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ESTIMATED_SEC_PER_SHEET = 42  # swap (~40s) + cut (~2s, Pillow). Sheets run
# sequentially (MAX_CONCURRENT_SHEETS=1) → lighter than mix-swap (no Replicate).


def _collect_sprite_scope(sprite: dict, force_resweep: bool) -> tuple[list[int], bool]:
    """Compute in-scope flat sheet indices + has_sheets.

    - original_crops empty → not in scope (handler marks `skipped`).
    - force_resweep=false AND any swap_result is_selected → idempotent skip.
    """
    sheets = sprite.get("crop_sheets") or []
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


async def _humans_by_id(remix_config: dict) -> dict[str, dict]:
    """Batch-resolve the humans referenced by the sprite's enabled config chars.

    image-api did `humans.select(...).in_(human_ids)`; the App DB has no per-book
    humans, so the adapter loads globally — we filter to the referenced ids.
    """
    rc_chars = remix_config.get("characters") or []
    human_ids = {
        c["human_id"]
        for c in rc_chars
        if isinstance(c, dict) and c.get("human_id")
    }
    if not human_ids:
        return {}
    want = {str(h) for h in human_ids}
    out: dict[str, dict] = {}
    for row in await get_adapter().list_humans(None):  # type: ignore[arg-type]
        if isinstance(row, dict) and str(row.get("id")) in want:
            out[str(row["id"])] = row
    return out


@router.post("/remix/{remix_id}/sprite-swap")
async def enqueue_remix_sprite_swap_endpoint(
    remix_id: str,
    body: RemixSpriteSwapEnqueueRequest,
    ctx: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()
    sprite_id = body.sprite_id

    # 0. Resolve per-job model selection (group 'swap') — raises UNSUPPORTED_MODEL
    #    (422, via RemixDomainError → dedicated handler) BEFORE any DB work.
    model_params = resolve_model_params(
        body.model_params.model_dump() if body.model_params else None, "swap"
    )
    logger.info(
        "sprite_swap_model_resolved remix_id=%s model=%s", remix_id, model_params["model"]
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

    remix_config = remix.get("remix_config") or {}
    sprites = remix.get("sprites") or []
    snapshot_id = remix.get("snapshot_id")
    if not snapshot_id:
        raise error_response(500, "INTERNAL_ERROR", "remix is missing snapshot_id")

    # 2. Resolve the sprite entry by id.
    sprite = find_sprite_by_id(sprites, sprite_id)
    if not sprite:
        raise error_response(404, "SPRITE_NOT_FOUND", f"sprite {sprite_id} not found")

    # 3. Resolve book_id + snapshot characters (no owner lookup).
    try:
        book_id = await adapter.get_book_id_for_remix(UUID(remix_id))
        snap = await adapter.get_current_snapshot(book_id, UUID(str(snapshot_id)))
        snap_characters = (snap or {}).get("characters") or []
    except Exception as exc:  # noqa: BLE001
        logger.exception("snapshot_lookup_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "snapshot lookup failed") from exc

    # 4. Resolve object pool (1× — lineup constant). Read the referenced humans.
    try:
        humans_by_id = await _humans_by_id(remix_config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("humans_load_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "humans lookup failed") from exc

    pool = resolve_sprite_object_map(
        sprite, remix_config, humans_by_id, snap_characters
    )

    # Precondition fail-loud (lineup constant → resolve once at enqueue).
    if not pool.lineup:
        raise error_response(
            422, "NO_SWAP_OBJECTS", "sprite has no character cell"
        )
    if pool.missing:
        raise error_response(
            422,
            "MISSING_OBJECT_CONFIG",
            "one or more sprite objects are missing remix_config",
            details={"object_keys": list(dict.fromkeys(pool.missing))},
        )

    # 5. Collect sheets in scope.
    in_scope, has_sheets = _collect_sprite_scope(sprite, body.force_resweep)
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

    # 6. Dedup — any active sprite-swap job for this remix (200 deduped, image-api
    #    parity; sprite dedup is 200 while mix-swap is 409 — see jobs-api.ts).
    try:
        existing = await adapter.find_active_job(UUID(remix_id), JOB_TYPE_SPRITE_SWAP)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dedup_check_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "dedup lookup failed") from exc

    if existing:
        return {
            "success": True,
            "data": {
                "deduped": True,
                "job_id": str(existing["id"]),
                "status": existing["status"],
                "type": existing.get("type"),
                "remix_id": remix_id,
                "active_swap_key": (existing.get("params") or {}).get("sprite_id"),
            },
        }

    # 7. Enqueue via jobs lib (user_id forced + params.source stamped inside).
    try:
        job = await enqueue(
            type=JOB_TYPE_SPRITE_SWAP,
            book_id=book_id,
            params={
                "remix_id": remix_id,
                "sprite_id": sprite_id,
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

    # 8. Init step_details (1 extra UPDATE; handler has a defensive rebuild).
    step_details = {"sheets": {str(i): "pending" for i in in_scope}}
    try:
        await adapter.update_job(job["id"], {"step_details": step_details})
    except Exception as exc:  # noqa: BLE001
        logger.warning("step_details_init_failed job_id=%s msg=%s", job["id"], exc)

    sheets_to_process = len(in_scope)

    audit(
        ctx,
        endpoint=f"jobs.{JOB_TYPE_SPRITE_SWAP}",
        resource_id=remix_id,
        job_id=str(job["id"]),
        type=JOB_TYPE_SPRITE_SWAP,
    )
    logger.info(
        "remix_sprite_swap_enqueued job_id=%s remix_id=%s sheets=%d objects=%d force_resweep=%s",
        job["id"], remix_id, sheets_to_process, pool.object_count, body.force_resweep,
    )

    # 9. Return 201.
    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": JOB_TYPE_SPRITE_SWAP,
                "remix_id": remix_id,
                "sprite_id": sprite_id,
                "object_count": pool.object_count,
                "total_steps": sheets_to_process,
                "sheets_to_process": sheets_to_process,
                "estimated_duration_sec": sheets_to_process * _ESTIMATED_SEC_PER_SHEET,
            },
        },
    )
