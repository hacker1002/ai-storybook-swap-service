"""Handler `remix_rmbg` (job 09) — remove the background of every in-scope crop
sheet of ONE batch entry (`remixes.rmbgs[]`, identified by `id`). Crop pipeline
stage 2: swap (`mixes[]`, job 05) → REMOVE-BG (this job) → upscale
(`upscales[]`, job 10).

Per sheet (locked in spec 09 + plan 260612-1438):
  1. compose PLAIN  `compose_crop_sheet()` in-process from `original_crops[]` +
                    `sheet_geometry`, frame `{draw_ordinals: false,
                    cell_stroke_width: 0}` — baked digits/strokes would survive
                    remove-bg and get cut into the pieces. Single-crop
                    fetch/decode failures are graceful (crop dropped from
                    cut/persist, `compose_skipped_count`++); ALL crops failing
                    → `ALL_CROPS_FAILED` (sheet-fatal, stage=compose).
  2. remove-bg      ONE `image_remove_bg_core` call per sheet (`imageBytes`
                    mode — bypasses the 10 MB decode cap; `preserveAlpha=true`,
                    `backgroundColor=null`, `return_bytes=true`). FAIL =
                    sheet-fatal `RMBG_FAILED` (no per-crop graceful fallback —
                    re-run is cheap, no Gemini cost).
  3. cut + upload   `cut_and_upload_native` on the RGBA sheet → pieces under
                    `rmbg-final/` (the composed sheet matches `sheet_geometry`
                    so scale ≈ 1 — the helper's rescale doubles as the
                    defensive guard for model dim drift). Lean output
                    `{spread_id, id, media_url}`. The RGBA sheet itself is
                    uploaded to `rmbg-sheets/` → `swap_results[].media_url`
                    (upload fail → null + warn, NOT fatal).

No AI swap primitive, no target pool — pure composer + remove-bg core + Pillow.
Sheets run SEQUENTIALLY (`MAX_CONCURRENT_SHEETS=1`, Replicate bound), then ONE
full-column `UPDATE remixes SET rmbgs=...` AFTER gather (single-writer; the
`rmbgs` column is disjoint from `mixes`/`upscales` → safe in parallel with jobs
05/10). Persist-fail → rollback `pre_state` + demote done sheets. Heartbeat #1
(`rmbg_done`, post remove-bg / pre-cut) honors cancel; finalize parity job 05.
`is_final` winner mutex per `(spread_id, id)` cross-batch WITHIN `rmbgs[]`
(per-stage R1) — finals of this stage feed the `upscales[]` batch build.

PII: crops carry real-person likeness (possibly children) — never log/echo
URLs, bytes, or base64; `step_details`/`errors[].message` carry only codes +
concise messages.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any

from src.core.job_types import JOB_TYPE_RMBG
from src.db.adapter import get_adapter
from src.jobs.helpers.promote_is_final import promote_is_final_for_sheet
from src.jobs.runner import JobContext, register
from src.services.ai_usage import AiCallContext
from src.models.jobs.remix_rmbg import (
    MAX_CONCURRENT_SHEETS,
    MAX_RESULT_ERRORS,
)
from src.models.requests.build_crop_sheet import BuildCropSheetRequest
from src.services.jobs.remix_rmbg_resolver import compose_crop_entries
from src.services.remix.crop_sheet_composer import compose_crop_sheet
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import find_batch_by_id
from src.services.remix.post_swap_pipeline import (
    PostSwapPipelineError,
    _now_path_segment,
    cut_and_upload_native,
)
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)

# Permanent prefixes inside the shared `storybook-assets` bucket (spec 09
# §Storage layout). The composed (pre-rmbg) sheet is NEVER uploaded — bytes
# pipe in-process straight into the remove-bg core.
STORAGE_RMBG_SHEET_PREFIX = "rmbg-sheets"
STORAGE_RMBG_CROP_PREFIX = "rmbg-final"

# Plain frame for the rmbg compose — NO ordinal badges, NO cell strokes
# (composer gates: `if stroke_w > 0` / `if draw_ordinals`, both resolved with
# `is not None` so explicit 0/False stick — verified plan Phase 02).
_PLAIN_FRAME: dict[str, Any] = {"draw_ordinals": False, "cell_stroke_width": 0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push_error(errors: list[dict], stage: str, **kw: Any) -> None:
    if len(errors) >= MAX_RESULT_ERRORS:
        if not any(e.get("code") == "TRUNCATED" for e in errors):
            errors.append(
                {
                    "stage": "internal",
                    "message": f"errors truncated at {MAX_RESULT_ERRORS}",
                    "code": "TRUNCATED",
                }
            )
        return
    entry: dict[str, Any] = {"stage": stage, "message": kw.pop("message", "?")}
    for k, v in kw.items():
        if v is not None:
            entry[k] = v
    errors.append(entry)


def _build_result(
    batch_id: str,
    processed: int,
    skipped: int,
    failed: int,
    errors: list[dict],
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "processed_sheets": processed,
        "skipped_sheets": skipped,
        "failed_sheets": failed,
        "errors": errors,
    }


@register(JOB_TYPE_RMBG)
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    batch_id: str = params["batch_id"]
    force_resweep: bool = bool(params.get("force_resweep", False))
    # D2 persist guarantees model_params present; D4 — read directly, NO fallback.
    # rmbg: public id == provider (owner/name) → forwarded straight to Replicate.
    rmbg_model: str = params["model_params"]["model"]

    # AI-usage attribution (Phase 05): built from the JOB ROW (NOT JobContext). The
    # `remix_id` routes the Replicate remove-bg cost to the remix billing bucket.
    ai_ctx = AiCallContext(
        job_id=job["id"],
        user_id=job.get("user_id"),
        book_id=job.get("book_id"),
        remix_id=(job.get("params") or {}).get("remix_id"),
        snapshot_id=(job.get("params") or {}).get("snapshot_id"),
        admin_ref=(job.get("params") or {}).get("admin_ref"),
        sid=(job.get("params") or {}).get("sid"),
    )

    adapter = get_adapter()
    errors: list[dict] = []
    processed = skipped = failed = done_count = 0

    # ── Load remix fresh (full row → the column this job owns + snapshot_id) ──
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "internal", message=f"remix load failed: {exc}")
        return ("failed", _build_result(batch_id, 0, 0, 0, errors))

    if not remix:
        _push_error(errors, "internal", message="remix_not_found")
        return ("failed", _build_result(batch_id, 0, 0, 0, errors))

    rmbgs: list = remix.get("rmbgs") or []
    batch = find_batch_by_id(rmbgs, batch_id)
    if batch is None:
        _push_error(errors, "internal", message="batch_not_found")
        return ("failed", _build_result(batch_id, 0, 0, 0, errors))

    try:
        batch_idx = rmbgs.index(batch)
    except ValueError:
        batch_idx = -1

    # ── step_details + scope (defensive rebuild) ─────────────────────
    step_details: dict[str, Any] = job.get("step_details") or {}
    sheets_block = step_details.get("sheets")
    if not isinstance(sheets_block, dict):
        sheets_block = {}
        step_details["sheets"] = sheets_block
    step_timings = step_details.get("step_timings")
    if not isinstance(step_timings, dict):
        step_timings = {}
        step_details["step_timings"] = step_timings

    crop_sheets: list = batch.get("crop_sheets") or []
    if not sheets_block:
        # Enqueue init failed — rebuild scope from the batch's sheets.
        for i, sheet in enumerate(crop_sheets):
            if not isinstance(sheet, dict) or not sheet.get("original_crops"):
                continue
            if not force_resweep and any(
                isinstance(r, dict) and r.get("is_selected")
                for r in (sheet.get("swap_results") or [])
            ):
                continue
            sheets_block[str(i)] = "pending"

    scope_indices = [int(k) for k in sheets_block.keys()]
    if not scope_indices:
        return ("completed", _build_result(batch_id, 0, 0, 0, errors))

    logger.info(
        "remix_rmbg_start job_id=%s remix_id=%s sheets=%d force_resweep=%s",
        ctx.id, remix_id, len(scope_indices), force_resweep,
    )

    if await ctx.check_cancel():
        return ("cancelled", _build_result(batch_id, 0, 0, 0, errors))

    report_lock = asyncio.Lock()
    sheet_sem = asyncio.Semaphore(MAX_CONCURRENT_SHEETS)

    async def do_sheet(sheet_index: int) -> None:
        nonlocal processed, skipped, failed, done_count
        async with sheet_sem:
            started = _now_iso()
            t0 = time.monotonic()
            sheet = crop_sheets[sheet_index]
            key = str(sheet_index)
            sheets_block[key] = "running"

            def _fail(stage: str, code: str | None, message: str) -> None:
                nonlocal failed
                sheets_block[key] = {
                    "state": "failed",
                    "stage": stage,
                    **({"code": code} if code else {}),
                    "message": message,
                }
                _push_error(
                    errors, stage, sheet_index=sheet_index, code=code, message=message
                )
                failed += 1
                logger.warning(
                    "rmbg_sheet_failed sheet_index=%d stage=%s code=%s",
                    sheet_index, stage, code,
                )

            try:
                stored_crops = sheet.get("original_crops") or []
                if not stored_crops:
                    sheets_block[key] = "skipped"
                    skipped += 1
                    return

                compose_crops, cut_crops = compose_crop_entries(stored_crops)
                if not compose_crops:
                    # Every stored crop is malformed → nothing composable.
                    _fail("compose", "ALL_CROPS_FAILED", "no composable crop")
                    return

                # ── (1) COMPOSE PLAIN (stage=compose) ───────────────────
                geom = sheet.get("sheet_geometry") or {}
                try:
                    compose_req = BuildCropSheetRequest(
                        sheet_geometry={
                            "width": int(geom.get("width") or 0),
                            "height": int(geom.get("height") or 0),
                        },
                        crops=compose_crops,
                        frame=_PLAIN_FRAME,
                    )
                    compose = await compose_crop_sheet(compose_req)
                except RemixDomainError as exc:
                    # ALL_CROPS_FAILED (every crop fetch/decode failed) or a
                    # request-shape violation (SHEET_TOO_LARGE, OUT_OF_BOUNDS…).
                    _fail("compose", exc.code, exc.message)
                    return
                except Exception as exc:  # noqa: BLE001 — pydantic/infra
                    _fail(
                        "compose", "ALL_CROPS_FAILED",
                        f"compose failed: {type(exc).__name__}",
                    )
                    return

                # Drop compose-skipped crops from cut/persist (their cell is
                # blank gutter — cutting it would persist an empty piece).
                compose_skipped_count = len(compose.skipped)
                if compose_skipped_count:
                    skipped_ids = {s.id for s in compose.skipped}
                    live_cut_crops = [
                        c for c in cut_crops if c["id"] not in skipped_ids
                    ]
                else:
                    live_cut_crops = cut_crops
                if not live_cut_crops:
                    _fail("compose", "ALL_CROPS_FAILED", "every crop skipped at compose")
                    return

                # ── (2) REMOVE-BG — ONE call per sheet (stage=rmbg) ─────
                # imageBytes mode (bypasses the 10 MB decode cap on large
                # sheets), transparent output, bytes back (no Storage hop).
                # Local import — avoids a hard import cycle at module load
                # (parity with the rev7 wrapper in post_swap_pipeline).
                from src.routers.retouch.image_remove_bg import (
                    ImageRemoveBgRequest,
                    image_remove_bg_core,
                )

                try:
                    rmbg_result = await image_remove_bg_core(
                        ImageRemoveBgRequest(
                            imageBytes=compose.png_bytes,
                            preserveAlpha=True,
                            backgroundColor=None,
                            return_bytes=True,
                            model=rmbg_model,
                        ),
                        ai_context=ai_ctx,
                    )
                    rgba_bytes = getattr(rmbg_result, "image_bytes", None)
                    if not rgba_bytes:
                        raise RuntimeError(
                            "remove-bg returned no bytes (return_bytes invariant)"
                        )
                except Exception as exc:  # noqa: BLE001 — sheet-fatal by design
                    logger.warning(
                        "rmbg_call_failed job_id=%s sheet_index=%d err_type=%s",
                        ctx.id, sheet_index, type(exc).__name__,
                    )
                    _fail("rmbg", "RMBG_FAILED", "remove-bg failed for sheet")
                    return

                # ── HEARTBEAT #1 (post-rmbg, pre-cut) + cancel check ────
                sheets_block[key] = "rmbg_done"
                async with report_lock:
                    await ctx.report(current_step=done_count, step_details=step_details)
                if await ctx.check_cancel():
                    sheets_block[key] = "cancelled"
                    skipped += 1
                    return

                # ── (3) CUT + UPLOAD pieces (stage=cut) ─────────────────
                # rmbg preserves the input dim → scale ≈ 1 (straight cut);
                # the helper's rescale-by-actual-dim doubles as the defensive
                # guard for dim drift (spec 09 OQ#4).
                try:
                    result_crops = await cut_and_upload_native(
                        rgba_bytes,
                        geom,
                        live_cut_crops,
                        sheet_idx=sheet_index,
                        storage_prefix=STORAGE_RMBG_CROP_PREFIX,
                    )
                except PostSwapPipelineError as exc:
                    _fail("cut", exc.code, exc.message)
                    return

                # Upload the bg-removed RGBA sheet (persisted artifact — spec
                # locked 2026-06-12). Upload fail → media_url null + warn,
                # NOT fatal (crops[] alone is sufficient downstream).
                sheet_url: str | None = None
                try:
                    sheet_url = await upload_bytes(
                        f"{STORAGE_RMBG_SHEET_PREFIX}/{_now_path_segment()}"
                        f"-{batch_id}-{sheet_index}.png",
                        rgba_bytes,
                        content_type="image/png",
                    )
                except StorageUploadError as exc:
                    logger.warning(
                        "rmbg_sheet_upload_fail job_id=%s sheet_index=%d reason=%s",
                        ctx.id, sheet_index, exc.reason,
                    )

                # ── APPLY in-memory (this task owns crop_sheets[idx]) ───
                if force_resweep:
                    sheet["swap_results"] = []
                swap_results = sheet.get("swap_results")
                if not isinstance(swap_results, list):
                    swap_results = []
                    sheet["swap_results"] = swap_results
                for r in swap_results:
                    if isinstance(r, dict):
                        r["is_selected"] = False
                swap_results.append(
                    {
                        "media_url": sheet_url,
                        "created_time": _now_iso(),
                        "is_selected": True,
                        "crops": result_crops,
                    }
                )
                # R1 winner mutex per (spread_id, id) cross-batch WITHIN the
                # rmbgs[] stage (the helper is stage-array generic).
                promote_stats = promote_is_final_for_sheet(
                    rmbgs, batch_idx, sheet_index
                )
                logger.info(
                    "rmbg_is_final_promote job_id=%s batch_idx=%d sheet_index=%d "
                    "promoted=%d cleared=%d",
                    ctx.id, batch_idx, sheet_index,
                    promote_stats["promoted_count"],
                    promote_stats["cleared_count"],
                )
                if compose_skipped_count:
                    sheets_block[key] = {
                        "state": "done",
                        "compose_skipped_count": compose_skipped_count,
                    }
                else:
                    sheets_block[key] = "done"
                processed += 1
            except Exception as exc:  # noqa: BLE001 — unexpected, never bubbles
                logger.exception(
                    "rmbg_sheet_unexpected job_id=%s sheet_index=%d",
                    ctx.id, sheet_index,
                )
                _fail("internal", None, f"unexpected: {type(exc).__name__}")
            finally:
                step_timings[key] = {
                    "started_at": started,
                    "duration_ms": round((time.monotonic() - t0) * 1000),
                }
                done_count += 1
                async with report_lock:
                    await ctx.report(current_step=done_count, step_details=step_details)

    # Pre-state snapshot for rollback-on-persist-fail.
    pre_state = {
        i: copy.deepcopy(crop_sheets[i].get("swap_results"))
        for i in scope_indices
        if 0 <= i < len(crop_sheets) and isinstance(crop_sheets[i], dict)
    }

    valid_indices = [i for i in scope_indices if 0 <= i < len(crop_sheets)]
    await asyncio.gather(*[do_sheet(i) for i in valid_indices])

    # ── 1 FULL-COLUMN write AFTER gather (single-writer, disjoint column) ──
    if processed > 0:
        try:
            await adapter.update_remix_job_column(remix_id, "rmbgs", rmbgs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("rmbg_persist_failed remix_id=%s", remix_id)
            for i in valid_indices:
                ikey = str(i)
                state = sheets_block.get(ikey)
                is_done = state == "done" or (
                    isinstance(state, dict) and state.get("state") == "done"
                )
                if is_done:
                    sheets_block[ikey] = {
                        "state": "failed",
                        "stage": "persist",
                        "message": "persist failed",
                    }
                    processed -= 1
                    failed += 1
                    _push_error(
                        errors, "persist", sheet_index=i,
                        message=f"persist failed: {type(exc).__name__}",
                    )
                if i in pre_state:
                    crop_sheets[i]["swap_results"] = pre_state[i]
            async with report_lock:
                await ctx.report(current_step=done_count, step_details=step_details)

    # Unconditional cancelled return on a late cancel (spec 09 §Flow — parity
    # job 05; committed work above stays persisted, result carries the counts).
    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(batch_id, processed, skipped, failed, errors),
        )

    logger.info(
        "remix_rmbg_done job_id=%s processed=%d skipped=%d failed=%d errors=%d",
        ctx.id, processed, skipped, failed, len(errors),
    )
    return (
        "completed",
        _build_result(batch_id, processed, skipped, failed, errors),
    )
