"""Handler `remix_upscale` (job 10) — upscale every crop of every in-scope crop
sheet of ONE batch entry (`remixes.upscales[]`, identified by `id`) to PRINT
300 DPI. Crop pipeline stage 3/FINAL: swap (job 05) → remove-bg (job 09) →
UPSCALE (this job). Finals of THIS stage are the Inject Phase 3 source.

Per crop, INDEPENDENTLY — NO combine, NO cut (upscaling changes per-piece
dims; `sheet_geometry` only organizes the UI sheet view):
  1. fetch    `original_crops[].media_url` (RGBA bg-removed piece @ NATIVE dim,
              SSRF-guarded) → measure (piece_w, piece_h).
  2. target   PRINT box from the MIX-stage layout geometry keyed
              `(spread_id, id)`: `mixes[].crop_sheets[].original_crops[]
              .geometry.{w,h}` is the FE layout engine's output in PX (canvas
              space = 1/4 print @300dpi) → `print = box ×
              PRINT_UPSCALE_FACTOR(4)`. ⚡2026-06-13 (user fix): the previous
              source — `illustration.spreads[].images[].geometry` — is
              %-OF-CANVAS, not px; using it as px shrank print targets to a
              few hundred px AND stretched pieces to the box ratio.
              `original_crops[].geometry` at THIS stage = native piece dims
              (NOT a layout box — never used for the target). Box missing
              (legacy row / crop absent from mixes[]) → UNIFORM
              ×PRINT_UPSCALE_FACTOR fallback (still upscaled, not skipped).
  3. scale    `max(print_w/piece_w, print_h/piece_h)`, clamp ≤
              MAX_UPSCALE_SCALE(10) + warn. The resize target is ALWAYS
              `piece_dims × scale` (UNIFORM — ratio preserved by
              construction; NEVER stretch to the box).
              `scale ≤ 1` → Pillow LANCZOS down (NO Replicate, rare).
  4. upscale  `run_upscale` (⚡2026-06-29 default xinntao/realesrgan Anime;
              `faceEnhance` is a GFPGAN NO-OP on the anime variant — pick
              real-esrgan/alexgenovese explicitly for post-swap face-restore)
              → LANCZOS normalize to the uniform target (esrgan rounding/tiling
              drift). Call fail →
              GRACEFUL fallback: keep pre-upscale bytes (NO resize),
              `upscale_skipped_count`++ — NEVER in `errors[]`, sheet proceeds.
  5. upload   `upscale-final/` (PERMANENT — fallback pieces too, the
              `rmbg-final/` inputs may get cleaned later). Lean persist
              `{spread_id, id, media_url, is_final?}`.

Sheet-fatal ONLY on `ALL_CROP_PIPELINES_FAILED` (every crop fetch/upload
failed — Storage outage) + `persist`. `swap_results[].media_url = null` (no
sheet output). Heartbeat after EVERY crop (Replicate calls are long; cancel is
honored at the SHEET boundary only — parity rev7). Sheets sequential ×
`MAX_CONCURRENT_UPSCALE_CROPS=1` → Replicate in-flight 1 inside this job;
cross-type contention with job 09 is bounded by the GLOBAL Replicate semaphore
(`services/replicate_client.py`). ONE full-column `UPDATE remixes SET
upscales=...` AFTER gather (disjoint column → safe in parallel with jobs
05/09); persist-fail → rollback `pre_state`. `is_final` winner mutex per
`(spread_id, id)` cross-batch WITHIN `upscales[]` (per-stage R1).

PII: crops carry real-person likeness (possibly children) — never log/echo
URLs, bytes, or base64 to stdout. EXCEPTION (⚡user decision 2026-06-13): the
LangSmith span `jobs.remix_upscale.crop` carries input/output media URLs so
upscale runs are reviewable in the LangSmith UI — trace ONLY, stdout stays
URL-free. The span is the SINGLE root trace per crop: it wraps the whole
per-crop pipeline, so `image.upscale` (run_upscale core) nests as a child run
instead of emitting a second root trace (dedup 2026-06-13).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any

from langsmith import trace as ls_trace

from src.db.adapter import get_adapter
from src.jobs.helpers.promote_is_final import promote_is_final_for_sheet
from src.jobs.runner import JobContext, register
from src.services.ai_usage import AiCallContext
from src.models.jobs.remix_upscale import (
    MAX_CONCURRENT_SHEETS,
    MAX_CONCURRENT_UPSCALE_CROPS,
    MAX_RESULT_ERRORS,
    MAX_UPSCALE_SCALE,
    PRINT_UPSCALE_FACTOR,
)
from src.models.requests.upscale_image import GRAIN_MAX_PIXELS, UpscaleCoreRequest
from src.services.http_fetch import fetch_image_bytes
from src.services.image.grain import apply_watercolor_grain
from src.services.image.upscale_core import run_upscale
from src.services.remix.mix_swap_resolver import find_batch_by_id
from src.services.remix.post_swap_pipeline import (
    _measure_sync,
    _now_path_segment,
    _resize_to_dim_sync,
)
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)

# Permanent prefix inside the shared `storybook-assets` bucket (spec 10
# §Storage layout) — the Inject Phase 3 source. No sheet path (media_url=null).
STORAGE_UPSCALE_CROP_PREFIX = "upscale-final"


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
    upscale_skipped_count: int,
    errors: list[dict],
    grain_skipped_count: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "batch_id": batch_id,
        "processed_sheets": processed,
        "skipped_sheets": skipped,
        "failed_sheets": failed,
        "upscale_skipped_count": upscale_skipped_count,
        "errors": errors,
    }
    # Only surface grain_skipped_count when grain was enabled for this job.
    if grain_skipped_count is not None:
        result["grain_skipped_count"] = grain_skipped_count
    return result


def _build_print_box_map(
    mixes: list | None,
) -> dict[tuple[str | None, str | None], tuple[float, float]]:
    """`(spread_id, id) → (box_w, box_h)` canvas-space PX from the MIX stage.

    ⚡2026-06-13 — print-target source moved OFF `illustration.spreads[]
    .images[].geometry`: that geometry is %-OF-CANVAS (snapshot spec
    `{w:100,h:100}`), not px — treating it as px shrank print targets AND
    stretched pieces to the wrong ratio. `mixes[].crop_sheets[]
    .original_crops[].geometry` is the FE layout engine's output in PX
    (canvas space) — the "canvas = 1/4 print @300dpi" anchor spec 10 intended.
    (spread_id, id) is invariant across the pipeline; w/h are re-pack
    invariant, so the first non-degenerate entry wins.
    """
    out: dict[tuple[str | None, str | None], tuple[float, float]] = {}
    for batch in mixes or []:
        if not isinstance(batch, dict):
            continue
        for sheet in batch.get("crop_sheets") or []:
            if not isinstance(sheet, dict):
                continue
            for crop in sheet.get("original_crops") or []:
                if not isinstance(crop, dict):
                    continue
                key = (crop.get("spread_id"), crop.get("id"))
                if key in out:
                    continue
                geom = crop.get("geometry")
                if not isinstance(geom, dict):
                    continue
                w = float(geom.get("w") or 0)
                h = float(geom.get("h") or 0)
                if w > 0 and h > 0:
                    out[key] = (w, h)
    return out


@register("remix_upscale")
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    batch_id: str = params["batch_id"]
    force_resweep: bool = bool(params.get("force_resweep", False))
    # D2 persist guarantees model_params present; D4 — read directly, NO fallback.
    # ⚡2026-06-29: all 4 allowlisted upscalers reach here (default xinntao Anime);
    # the persisted model_params.model is forwarded verbatim to the core.
    # `scale` is NEVER from model_params — always geometry-derived (print 300 DPI).
    _mp: dict = params["model_params"]
    upscale_model: str = _mp["model"]
    upscale_face_enhance: bool = bool((_mp.get("params") or {}).get("face_enhance", True))
    # Grain (top-level, model-agnostic) — normalized dict at enqueue, or None.
    # When set the job applies grain PER-CROP after normalize-resize; core grain
    # stays OFF (UpscaleCoreRequest(grain=None)) to avoid double-apply + resize.
    grain_cfg: dict | None = params.get("grain")

    # AI-usage attribution (Phase 05): built from the JOB ROW (NOT JobContext). The
    # `remix_id` routes the Replicate upscale cost to the remix billing bucket; the
    # `run_upscale(operation="remix.upscale")` below tags the row's operation.
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
    total_upscale_skipped = 0
    total_grain_skipped = 0

    # ── Load remix fresh (full row → owned column + mixes for print targets) ──
    try:
        remix = await adapter.get_remix(remix_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "internal", message=f"remix load failed: {exc}")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    if not remix:
        _push_error(errors, "internal", message="remix_not_found")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    upscales: list = remix.get("upscales") or []
    batch = find_batch_by_id(upscales, batch_id)
    if batch is None:
        _push_error(errors, "internal", message="batch_not_found")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    try:
        batch_idx = upscales.index(batch)
    except ValueError:
        batch_idx = -1

    print_box_map = _build_print_box_map(remix.get("mixes"))

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
        return ("completed", _build_result(batch_id, 0, 0, 0, 0, errors))

    logger.info(
        "remix_upscale_start job_id=%s remix_id=%s sheets=%d print_boxes=%d force_resweep=%s",
        ctx.id, remix_id, len(scope_indices), len(print_box_map), force_resweep,
    )

    if await ctx.check_cancel():
        return ("cancelled", _build_result(batch_id, 0, 0, 0, 0, errors))

    report_lock = asyncio.Lock()
    sheet_sem = asyncio.Semaphore(MAX_CONCURRENT_SHEETS)
    crop_sem = asyncio.Semaphore(MAX_CONCURRENT_UPSCALE_CROPS)

    async def do_sheet(sheet_index: int) -> None:
        nonlocal processed, skipped, failed, done_count
        nonlocal total_upscale_skipped, total_grain_skipped
        async with sheet_sem:
            started = _now_iso()
            t0 = time.monotonic()
            sheet = crop_sheets[sheet_index]
            key = str(sheet_index)
            sheets_block[key] = "running"
            upscale_skipped_count = 0
            grain_skipped_count = 0
            crops_done = 0

            try:
                stored_crops = [
                    c
                    for c in (sheet.get("original_crops") or [])
                    if isinstance(c, dict)
                    and isinstance(c.get("media_url"), str)
                    and c["media_url"].startswith("http")
                ]
                if not stored_crops:
                    sheets_block[key] = "skipped"
                    skipped += 1
                    return

                crops_total = len(stored_crops)

                async def do_crop(
                    crop_idx: int, crop: dict
                ) -> dict[str, Any] | None:
                    nonlocal upscale_skipped_count, grain_skipped_count, crops_done
                    async with crop_sem:
                        crop_id = crop.get("id") or f"crop-{crop_idx}"
                        try:
                            # SINGLE LangSmith root span per crop (⚡user
                            # decision 2026-06-13: carries input/output URLs —
                            # trace ONLY, stdout stays URL-free). run_upscale's
                            # `image.upscale` nests as a CHILD run via the
                            # tracing contextvar instead of a 2nd root trace.
                            # No-op without a LangSmith key.
                            # MUST be the SYNC `with` (langsmith 0.7.x bug:
                            # `async with` hands its ctx to `aio_to_thread` as
                            # `__ctx or copy_context()` — the job task's EMPTY
                            # Context is falsy → discarded → the parent
                            # contextvar set by _setup never copies back and
                            # child runs detach into separate root traces).
                            # Sync `with` sets the contextvar directly. Safe
                            # in async: post()/patch() only enqueue to the
                            # background batch queue. Regression-guarded by
                            # TestLangSmithTraceNesting.
                            with ls_trace(
                                name="jobs.remix_upscale.crop",
                                inputs={
                                    "input_url": crop.get("media_url"),
                                    "spread_id": crop.get("spread_id"),
                                    "crop_id": crop.get("id"),
                                },
                            ) as crop_run:
                                # (1) fetch the RGBA piece (SSRF-guarded).
                                piece_bytes, _ct = await fetch_image_bytes(
                                    crop["media_url"]
                                )
                                piece_w, piece_h = await asyncio.to_thread(
                                    _measure_sync, piece_bytes
                                )

                                # (2) print box from the MIX-stage layout geometry
                                # (canvas-space px — ⚡2026-06-13 fix: illustration
                                # geometry is %-based; treating it as px distorted
                                # ratio). Missing box → uniform ×4 fallback.
                                box = print_box_map.get(
                                    (crop.get("spread_id"), crop.get("id"))
                                )
                                if box is None:
                                    scale = float(PRINT_UPSCALE_FACTOR)
                                    logger.info(
                                        "upscale_no_print_box sheet_idx=%d crop_idx=%d fallback_scale=%.1f",
                                        sheet_index, crop_idx, scale,
                                    )
                                else:
                                    box_w, box_h = box
                                    scale = max(
                                        box_w * PRINT_UPSCALE_FACTOR / max(1, piece_w),
                                        box_h * PRINT_UPSCALE_FACTOR / max(1, piece_h),
                                    )
                                if scale > MAX_UPSCALE_SCALE:
                                    logger.warning(
                                        "upscale_scale_clamp sheet_idx=%d crop_idx=%d scale=%.3f clamp=%.1f",
                                        sheet_index, crop_idx, scale, MAX_UPSCALE_SCALE,
                                    )
                                    scale = MAX_UPSCALE_SCALE

                                # (3) UNIFORM target from the PIECE dims — ratio
                                # preserved by construction (NEVER stretch to box).
                                target_w = max(1, round(piece_w * scale))
                                target_h = max(1, round(piece_h * scale))

                                if scale <= 1.0:
                                    if (target_w, target_h) == (piece_w, piece_h):
                                        final_bytes = piece_bytes
                                        mode = "verbatim"
                                    else:
                                        # Piece already ≥ print target — Pillow
                                        # LANCZOS down, NO Replicate.
                                        final_bytes = await asyncio.to_thread(
                                            _resize_to_dim_sync,
                                            piece_bytes,
                                            (target_w, target_h),
                                        )
                                        mode = "downscaled"
                                else:
                                    # (4) real-esrgan + LANCZOS normalize to the
                                    # uniform target (rounding/tiling drift only —
                                    # esrgan preserves the input ratio).
                                    try:
                                        up_result = await run_upscale(
                                            UpscaleCoreRequest(
                                                imageBytes=piece_bytes,
                                                scale=scale,  # geometry-derived (print), NEVER model_params
                                                faceEnhance=upscale_face_enhance,
                                                return_bytes=True,
                                                model=upscale_model,
                                                grain=None,  # core grain OFF — job grains per-crop below (no double-apply / no resize)
                                            ),
                                            ai_context=ai_ctx,
                                            operation="remix.upscale",
                                        )
                                        up_bytes = getattr(
                                            up_result, "image_bytes", None
                                        )
                                        if not up_bytes:
                                            raise RuntimeError(
                                                "upscale returned no bytes"
                                            )
                                        final_bytes = await asyncio.to_thread(
                                            _resize_to_dim_sync,
                                            up_bytes,
                                            (target_w, target_h),
                                        )
                                        mode = "upscaled"
                                    except Exception as exc:  # noqa: BLE001
                                        # Graceful fallback (locked 2026-05-29):
                                        # keep pre-upscale bytes, NO resize —
                                        # this crop exports below 300 DPI.
                                        logger.warning(
                                            "upscale_call_fallback sheet_idx=%d crop_idx=%d err_type=%s",
                                            sheet_index, crop_idx,
                                            type(exc).__name__,
                                        )
                                        final_bytes = piece_bytes
                                        target_w, target_h = piece_w, piece_h
                                        upscale_skipped_count += 1
                                        mode = "fallback-verbatim"

                                # (4b) WATERCOLOR GRAIN — per-crop, on the FINAL
                                # normalized bytes (every mode), so grain is never
                                # resized/blurred and never double-applied (core
                                # grain is OFF). Per-crop seed = base + crop_idx →
                                # distinct pattern per cell, still deterministic.
                                # Non-fatal: fail / over-cap → keep ungrained
                                # bytes + grain_skipped_count++ (dims unchanged).
                                grain_applied_here = False
                                grain_seed_used: int | None = None
                                if grain_cfg is not None:
                                    out_px = target_w * target_h
                                    if out_px <= GRAIN_MAX_PIXELS:
                                        grain_seed_used = (
                                            int(grain_cfg["seed"]) + crop_idx
                                        )
                                        try:
                                            final_bytes = await asyncio.to_thread(
                                                apply_watercolor_grain,
                                                final_bytes,
                                                amp=grain_cfg["amp"],
                                                blur=grain_cfg["blur"],
                                                seed=grain_seed_used,
                                            )
                                            grain_applied_here = True
                                        except Exception as gexc:  # noqa: BLE001
                                            logger.warning(
                                                "upscale_grain_failed sheet_idx=%d crop_idx=%d err_type=%s",
                                                sheet_index, crop_idx,
                                                type(gexc).__name__,
                                            )
                                            grain_skipped_count += 1
                                    else:
                                        logger.warning(
                                            "upscale_grain_skip_over_cap sheet_idx=%d crop_idx=%d out_px=%d cap=%d",
                                            sheet_index, crop_idx, out_px,
                                            GRAIN_MAX_PIXELS,
                                        )
                                        grain_skipped_count += 1

                                # (5) upload final → upscale-final/ (PERMANENT).
                                media_url = await upload_bytes(
                                    f"{STORAGE_UPSCALE_CROP_PREFIX}/"
                                    f"{_now_path_segment()}-{sheet_index}-{crop_id}.png",
                                    final_bytes,
                                    content_type="image/png",
                                )
                                crop_run.end(
                                    outputs={
                                        "output_url": media_url,
                                        "width": target_w,
                                        "height": target_h,
                                        "piece_w": piece_w,
                                        "piece_h": piece_h,
                                        "scale": scale,
                                        "mode": mode,
                                        "grain": {
                                            "applied": grain_applied_here,
                                            "amp": grain_cfg["amp"],
                                            "blur": grain_cfg["blur"],
                                            "seed": grain_seed_used,
                                        }
                                        if grain_cfg is not None
                                        else None,
                                    }
                                )
                                return {
                                    "spread_id": crop.get("spread_id"),
                                    "id": crop_id,
                                    "media_url": media_url,
                                }
                        except Exception as exc:  # noqa: BLE001 — fetch/upload
                            # Drop this crop (sheet proceeds when ≥1 OK).
                            logger.warning(
                                "upscale_crop_dropped sheet_idx=%d crop_idx=%d err_type=%s",
                                sheet_index, crop_idx, type(exc).__name__,
                            )
                            return None
                        finally:
                            # HEARTBEAT per-crop (Replicate calls are long —
                            # reaper-safe). NO cancel check here: cancel is
                            # honored at the sheet boundary (parity rev7).
                            crops_done += 1
                            sheets_block[key] = {
                                "state": "processing",
                                "crops_done": crops_done,
                                "crops_total": crops_total,
                            }
                            async with report_lock:
                                await ctx.report(
                                    current_step=done_count,
                                    step_details=step_details,
                                )

                crop_results = await asyncio.gather(
                    *[do_crop(i, c) for i, c in enumerate(stored_crops)]
                )
                result_crops = [r for r in crop_results if r is not None]
                if not result_crops:
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "crops",
                        "code": "ALL_CROP_PIPELINES_FAILED",
                        "message": "every crop fetch/upload failed",
                    }
                    _push_error(
                        errors,
                        "crops",
                        sheet_index=sheet_index,
                        code="ALL_CROP_PIPELINES_FAILED",
                        message="every crop fetch/upload failed",
                    )
                    failed += 1
                    return

                # ── Cancel check at the SHEET boundary (post crop loop) ──
                if await ctx.check_cancel():
                    sheets_block[key] = "cancelled"
                    skipped += 1
                    return

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
                        # No sheet output — per-crop processing (spec locked).
                        # The UI builds the sheet view on demand from crops[].
                        "media_url": None,
                        "created_time": _now_iso(),
                        "is_selected": True,
                        "crops": result_crops,
                    }
                )
                # R1 winner mutex per (spread_id, id) cross-batch WITHIN the
                # upscales[] stage — finals here are the Inject Phase 3 source.
                promote_stats = promote_is_final_for_sheet(
                    upscales, batch_idx, sheet_index
                )
                logger.info(
                    "upscale_is_final_promote job_id=%s batch_idx=%d sheet_index=%d "
                    "promoted=%d cleared=%d",
                    ctx.id, batch_idx, sheet_index,
                    promote_stats["promoted_count"],
                    promote_stats["cleared_count"],
                )
                total_upscale_skipped += upscale_skipped_count
                total_grain_skipped += grain_skipped_count
                if upscale_skipped_count:
                    sheets_block[key] = {
                        "state": "done",
                        "upscale_skipped_count": upscale_skipped_count,
                    }
                else:
                    sheets_block[key] = "done"
                processed += 1
            except Exception as exc:  # noqa: BLE001 — unexpected, never bubbles
                logger.exception(
                    "upscale_sheet_unexpected job_id=%s sheet_index=%d",
                    ctx.id, sheet_index,
                )
                sheets_block[key] = {
                    "state": "failed",
                    "stage": "internal",
                    "message": f"unexpected: {type(exc).__name__}",
                }
                _push_error(
                    errors,
                    "internal",
                    sheet_index=sheet_index,
                    message=f"unexpected: {type(exc).__name__}",
                )
                failed += 1
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
            await adapter.update_remix_job_column(remix_id, "upscales", upscales)
        except Exception as exc:  # noqa: BLE001
            logger.exception("upscale_persist_failed remix_id=%s", remix_id)
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

    # Unconditional cancelled return on a late cancel (spec 10 §Flow — parity
    # job 05; committed work above stays persisted, result carries the counts).
    grain_count = total_grain_skipped if grain_cfg is not None else None
    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(
                batch_id, processed, skipped, failed, total_upscale_skipped,
                errors, grain_skipped_count=grain_count,
            ),
        )

    logger.info(
        "remix_upscale_done job_id=%s processed=%d skipped=%d failed=%d upscale_skipped=%d grain_skipped=%s errors=%d",
        ctx.id, processed, skipped, failed, total_upscale_skipped, grain_count, len(errors),
    )
    return (
        "completed",
        _build_result(
            batch_id, processed, skipped, failed, total_upscale_skipped,
            errors, grain_skipped_count=grain_count,
        ),
    )
