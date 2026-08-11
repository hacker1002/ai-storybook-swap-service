"""POST /api/jobs/remix/{remix_id}/audio-swap — enqueue audio-swap job.

Ported from image-api `src/routers/jobs/enqueue_remix_audio_swap.py`. Regenerates
dirty TTS chunks + rebuilds combined audio per textbox/language for every spread
in scope. The 201 success + 200 skipped + 200 dedup bodies are byte-identical to
image-api (FE `jobs-api.ts` parses them).

Service deltas vs image-api (same as sprite/mix-swap): editor-session Bearer auth
(no X-API-Key), existence check instead of owner lookup, `admin_ref`/`sid` stamped
into params, DB via `get_adapter()`, `enqueue` without `user_id`.

Returns:
  - 201 + success data on enqueue.
  - 200 + skipped data when precheck finds 0 dirty chunks (no row created).
  - 200 + dedup data when an active audio-swap job already exists for this remix.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.audit import audit
from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.job_types import JOB_TYPE_AUDIO_SWAP
from src.db.adapter import get_adapter
from src.jobs import enqueue
from src.models.jobs.remix_audio_swap import RemixAudioSwapEnqueueRequest
from src.routers._shared.deps import error_response
from src.services.remix_voice_resolver import iter_enabled_languages, needs_regen

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/remix/{remix_id}/audio-swap")
async def enqueue_remix_audio_swap_endpoint(
    remix_id: str,
    body: RemixAudioSwapEnqueueRequest,
    ctx: EditorSessionContext = Depends(require_editor_session),
):
    adapter = get_adapter()

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
    illustration = remix.get("illustration") or {}
    spreads = illustration.get("spreads") or []
    snapshot_id = remix.get("snapshot_id")
    if not snapshot_id:
        raise error_response(500, "INTERNAL_ERROR", "remix is missing snapshot_id")

    # 2. Resolve book_id (cost attribution; no owner lookup).
    try:
        book_id = await adapter.get_book_id_for_remix(UUID(remix_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("book_lookup_failed remix_id=%s", remix_id)
        raise error_response(500, "INTERNAL_ERROR", "book lookup failed") from exc

    # 3. Precheck.
    langs = iter_enabled_languages(remix_config)
    chunks_to_regen = 0
    textboxes_to_recombine = 0
    spreads_in_scope: list[str] = []

    for spread in spreads:
        if not isinstance(spread, dict):
            continue
        spread_dirty = False
        for tb in spread.get("textboxes") or []:
            if not isinstance(tb, dict):
                continue
            for lang in langs:
                lang_block = tb.get(lang)
                if not isinstance(lang_block, dict):
                    continue
                audio = lang_block.get("audio")
                if not isinstance(audio, dict):
                    continue
                chunks = audio.get("chunks") or []
                if not chunks:
                    continue
                dirty_in_tb = 0
                for c in chunks:
                    if isinstance(c, dict) and needs_regen(c, remix_config):
                        chunks_to_regen += 1
                        dirty_in_tb += 1
                if dirty_in_tb > 0:
                    textboxes_to_recombine += 1
                    spread_dirty = True
        if spread_dirty:
            spread_id = spread.get("id")
            if spread_id:
                spreads_in_scope.append(spread_id)

    if chunks_to_regen == 0:
        return {
            "success": True,
            "data": {
                "skipped": True,
                "reason": "no_chunks_need_regen",
                "chunks_to_regen": 0,
            },
        }

    # 4. Dedup — active audio-swap job for this remix (200 deduped, image-api parity).
    try:
        existing = await adapter.find_active_job(UUID(remix_id), JOB_TYPE_AUDIO_SWAP)
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
                "type": JOB_TYPE_AUDIO_SWAP,
                "remix_id": remix_id,
            },
        }

    # 5. Enqueue via jobs lib (user_id forced + params.source stamped inside).
    try:
        job = await enqueue(
            type=JOB_TYPE_AUDIO_SWAP,
            book_id=book_id,
            params={
                "remix_id": remix_id,
                "triggered_by": body.triggered_by,
                "max_concurrent_chunks_per_textbox": body.max_concurrent_chunks_per_textbox,
                "admin_ref": ctx.admin_ref,
                "sid": ctx.sid,
            },
            total_steps=len(spreads_in_scope),
        )
    except ValueError as exc:
        raise error_response(500, "JOB_INSERT_FAILED", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("enqueue_failed remix_id=%s", remix_id)
        raise error_response(500, "JOB_INSERT_FAILED", str(exc)) from exc

    # 6. Init step_details (1 extra UPDATE, KISS).
    step_details = {"spreads": {sid: "pending" for sid in spreads_in_scope}}
    try:
        await adapter.update_job(job["id"], {"step_details": step_details})
    except Exception as exc:  # noqa: BLE001
        # Handler has a defensive rebuild path; non-fatal.
        logger.warning("step_details_init_failed job_id=%s msg=%s", job["id"], exc)

    audit(
        ctx,
        "POST /api/jobs/remix/{remix_id}/audio-swap",
        remix_id,
        job_id=str(job["id"]),
        type=JOB_TYPE_AUDIO_SWAP,
    )
    logger.info(
        "remix_audio_swap_enqueued job_id=%s remix_id=%s total_steps=%d chunks=%d textboxes=%d triggered_by=%s",
        job["id"],
        remix_id,
        len(spreads_in_scope),
        chunks_to_regen,
        textboxes_to_recombine,
        body.triggered_by,
    )

    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "job_id": str(job["id"]),
                "status": "queued",
                "type": JOB_TYPE_AUDIO_SWAP,
                "remix_id": remix_id,
                "total_steps": len(spreads_in_scope),
                "chunks_to_regen": chunks_to_regen,
                "textboxes_to_recombine": textboxes_to_recombine,
                "skipped": False,
            },
        },
    )
