"""Handler `remix_sprite_swap` — swap every in-scope crop sheet of ONE sprite
entry (`remixes.sprites[]`, identified by `id`) using the per-object per-trait AI
primitive `run_swap_sprite_sheet`, then a CUT-ONLY post-swap pipeline, writing
`swap_results[]` with `crops[]` + the R1 `is_final` winner mutex.

Ported VERBATIM from image-api `src/jobs/handlers/remix_sprite_swap.py` (P3b). The
ONLY changes are the I/O seams:
  - `get_supabase_client()` reads → `src.db.adapter.get_adapter()` asyncpg calls
    (`get_remix`, `get_current_snapshot`, `list_humans`, `update_remix_columns`);
  - `AiCallContext` threads audit `admin_ref`/`sid` from `job.params` (stamped by
    the enqueue route) and pins `user_id=None` (this service has no user directory).
The step order, EVERY `check_cancel` call-site, `result`/`step_details` structure
and `JobError.stage` values are preserved exactly.

PII: never log/echo URLs, names, or base64. `step_details`/`errors[].message`
carry only codes + concise messages + `object_key` (entity key, non-PII).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.db.adapter import get_adapter
from src.jobs.runner import JobContext, register
from src.core.job_types import JOB_TYPE_SPRITE_SWAP
from src.services.ai_usage import AiCallContext
from src.models.jobs.remix_sprite_swap import (
    MAX_CONCURRENT_SHEETS,
    MAX_RESULT_ERRORS,
)
from src.models.requests.swap_sprite_sheet import (
    MAX_SWAP_OBJECTS,
    SwapSpriteSheetCoreRequest,
)
from src.services.remix.detect_crop_geometry_service import detect_boxes_for_cut
from src.services.remix.errors import RemixDomainError
from src.services.remix.post_swap_pipeline import PostSwapPipelineError
from src.services.remix.sprite_cut import (
    STORAGE_SPRITE_CROP_PREFIX,  # noqa: F401 — re-export for tests/parity
    cut_sprite_sheet_to_crops,
)
from src.services.remix.sprite_swap_resolver import (
    clear_foreign_finals,
    find_sprite_by_id,
    resolve_sprite_object_map,
    select_sheet_objects,
)
from src.services.remix.sprite_swap_resolver import (
    eligible_sheet_crops as _eligible_sheet_crops,
)
from src.services.remix.swap_image_helpers import build_dated_path
from src.services.remix.swap_sprite_sheet_core import (
    STORAGE_SWAP_PREFIX,
    run_swap_sprite_sheet,
)
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)

# Maps `RemixDomainError.code` raised by `run_swap_sprite_sheet` to the stage
# enum. The cut helper raises `PostSwapPipelineError` (stage=cut) read directly.
_COMPOSE_CODES = {"ALL_CROPS_FAILED"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map_code_to_stage(code: str | None) -> str:
    if code in _COMPOSE_CODES:
        return "compose"
    return "swap"


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
    sprite_id: str,
    object_count: int,
    swapped: int,
    skipped: int,
    failed: int,
    errors: list[dict],
) -> dict[str, Any]:
    return {
        "sprite_id": sprite_id,
        "object_count": object_count,
        "swapped_sheets": swapped,
        "skipped_sheets": skipped,
        "failed_sheets": failed,
        "errors": errors,
    }


def _build_core_request(
    sheet: dict,
    sheet_objects: list[dict],
    eligible_crops: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> SwapSpriteSheetCoreRequest:
    """Project a stored sprite crop sheet into the per-object per-trait primitive's
    request.

    `eligible_crops` (from `_eligible_sheet_crops`) is the single filtered+ordered
    crop list — already the `SpriteSheetCrop` shape, so it maps 1:1. `swap_objects`
    = the per-sheet subset. `return_bytes=True` → the core returns raw PNG bytes for
    in-process cut. `model`/`temperature` come from the resolved
    `job.params.model_params` (public id; None → core defaults → parity).
    """
    geom = sheet.get("sheet_geometry") or {}
    return SwapSpriteSheetCoreRequest(
        sheet_geometry={
            "width": int(geom.get("width", 0)),
            "height": int(geom.get("height", 0)),
        },
        crops=eligible_crops,
        swap_objects=sheet_objects,
        # ⚡detect-delegate (2026-06-26): composed sprite sheet (Ảnh #1) is the
        # original-scene reference for detect-crop-geometry → core uploads it.
        return_composed_sheet=True,
        return_bytes=True,
        model=model,
        temperature=temperature,
    )


def _build_cut_crops(eligible_crops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slim the eligible crops into the dicts `cut_sprite_sheet_to_crops` reads.

    Derived from the SAME `_eligible_sheet_crops` list the composer used (slice
    alignment + no blank-final clobber — see `_eligible_sheet_crops`). The cut
    helper echoes `type/object_key/variant_key/geometry` via `.get()` (handoff
    Phase 02) → these MUST be populated. `geometry` is the ORIGINAL cell geometry
    (the helper rescales to native dim internally + resizes back to it).
    """
    return [
        {
            "type": c["type"],
            "object_key": c["object_key"],
            "variant_key": c["variant_key"],
            "geometry": c["geometry"],
        }
        for c in eligible_crops
    ]


def _recognition_hint_sprite(crop: dict[str, Any]) -> str | None:
    """Scene-recognition hint for detect-crop-geometry (sprite): object + variant
    labels. Sprite cells of the SAME character differ only by pose/variant, so a
    non-empty hint is important to disambiguate. None when keys are missing."""
    parts = [str(crop.get(k)).strip() for k in ("object_key", "variant_key") if crop.get(k)]
    hint = ", ".join(p for p in parts if p)
    return hint[:500] or None


@register(JOB_TYPE_SPRITE_SWAP)
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    sprite_id: str = params["sprite_id"]
    force_resweep: bool = bool(params.get("force_resweep", False))
    # D2 persist guarantees model_params present on every post-ship job row; D4 —
    # read directly, NO defensive registry-default fallback (back-compat risk
    # ACCEPTED: dev env, ephemeral queue, no pre-ship in-flight jobs).
    model_params: dict = params["model_params"]
    swap_model: str = model_params["model"]
    swap_temperature = (model_params.get("params") or {}).get("temperature")

    # AI-usage attribution (Phase 05): built from the JOB ROW (NOT JobContext). The
    # `remix_id` routes the Gemini swap cost to the remix billing bucket. Audit
    # `admin_ref`/`sid` are stamped into `params` by the enqueue route (role-wide
    # session — the only actor attribution this service has). `user_id` is None
    # (App DB has no user directory).
    ai_ctx = AiCallContext(
        job_id=job["id"],
        user_id=None,
        book_id=job.get("book_id"),
        remix_id=(job.get("params") or {}).get("remix_id"),
        snapshot_id=(job.get("params") or {}).get("snapshot_id"),
        admin_ref=params.get("admin_ref"),
        sid=params.get("sid"),
    )

    adapter = get_adapter()
    errors: list[dict] = []
    swapped = skipped = failed = done_count = 0

    # ── Load remix fresh ─────────────────────────────────────────────
    try:
        remix = await adapter.get_remix(UUID(remix_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "resolve", message=f"remix load failed: {exc}")
        return ("failed", _build_result(sprite_id, 0, 0, 0, 0, errors))

    if not remix:
        _push_error(errors, "resolve", message="remix_not_found")
        return ("failed", _build_result(sprite_id, 0, 0, 0, 0, errors))

    remix_config: dict = remix.get("remix_config") or {}
    sprites: list = remix.get("sprites") or []
    snapshot_id = remix.get("snapshot_id")

    sprite = find_sprite_by_id(sprites, sprite_id)
    if sprite is None:
        _push_error(errors, "resolve", message="sprite_not_found")
        return ("failed", _build_result(sprite_id, 0, 0, 0, 0, errors))

    # ── Load snapshot characters + humans → resolve object pool ONCE ──
    try:
        snap_characters: list = []
        if snapshot_id:
            snap = await adapter.get_current_snapshot(UUID(int=0), UUID(str(snapshot_id)))
            snap_characters = (snap or {}).get("characters") or []

        # Collect the human_ids referenced by the sprite's enabled config chars,
        # then one batched read of the humans rows (visual_profiles inline JSONB).
        rc_chars = remix_config.get("characters") or []
        human_ids = sorted(
            {
                c["human_id"]
                for c in rc_chars
                if isinstance(c, dict) and c.get("human_id")
            }
        )
        humans_by_id: dict[str, dict] = {}
        if human_ids:
            wanted = set(human_ids)
            all_humans = await adapter.list_humans(UUID(int=0))
            for row in all_humans or []:
                if isinstance(row, dict) and row.get("id") is not None:
                    rid = str(row["id"])
                    if rid in wanted:
                        humans_by_id[rid] = row

        pool = resolve_sprite_object_map(
            sprite, remix_config, humans_by_id, snap_characters
        )
        if not pool.lineup:
            raise RemixDomainError(
                status=422,
                code="NO_SWAP_OBJECTS",
                message="sprite has no character cell",
            )
        if pool.missing:
            raise RemixDomainError(
                status=422,
                code="MISSING_OBJECT_CONFIG",
                message="one or more sprite objects are missing remix_config",
                details={"object_keys": pool.missing},
            )
        object_map = pool.object_map
    except RemixDomainError as exc:
        _push_error(errors, "resolve", code=exc.code, message=exc.message)
        return ("failed", _build_result(sprite_id, 0, 0, 0, 0, errors))
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve_object_map_failed remix_id=%s", remix_id)
        _push_error(errors, "resolve", message=f"resolve failed: {exc}")
        return ("failed", _build_result(sprite_id, 0, 0, 0, 0, errors))

    object_count = pool.object_count

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

    crop_sheets: list = sprite.get("crop_sheets") or []
    if not sheets_block:
        # Enqueue init failed — rebuild scope from the sprite's sheets.
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
        return (
            "completed",
            _build_result(sprite_id, object_count, 0, 0, 0, errors),
        )

    logger.info(
        "remix_sprite_swap_start job_id=%s remix_id=%s sheets=%d objects=%d force_resweep=%s",
        ctx.id, remix_id, len(scope_indices), object_count, force_resweep,
    )

    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(sprite_id, object_count, 0, 0, 0, errors),
        )

    report_lock = asyncio.Lock()
    sheet_sem = asyncio.Semaphore(MAX_CONCURRENT_SHEETS)

    async def do_sheet(sheet_index: int) -> None:
        nonlocal swapped, skipped, failed, done_count
        async with sheet_sem:
            started = _now_iso()
            t0 = time.monotonic()
            sheet = crop_sheets[sheet_index]
            key = str(sheet_index)
            sheets_block[key] = "running"

            async def _emit_heartbeat(stage_label: str) -> bool:
                """Mid-pipeline heartbeat: bump updated_at + poll cancel.

                Returns True iff cancel was requested — caller must set
                `sheets_block[key]='cancelled'`, inc `skipped`, and return.
                """
                sheets_block[key] = stage_label
                async with report_lock:
                    await ctx.report(current_step=done_count, step_details=step_details)
                return await ctx.check_cancel()

            try:
                if not sheet.get("original_crops"):
                    sheets_block[key] = "skipped"
                    skipped += 1
                    return

                # ── Per-sheet object subset: swap ONLY objects present in THIS
                #    sheet's crops (intersect pool ∩ cells.object_key). ─────────
                sheet_objects = select_sheet_objects(object_map, sheet)
                if not sheet_objects:
                    # Sheet has crops but no resolvable character object → skip.
                    sheets_block[key] = "skipped"
                    skipped += 1
                    return
                # Per-sheet budget (real Gemini per-call ceiling) — fail this
                # sheet only; siblings proceed.
                if len(sheet_objects) > MAX_SWAP_OBJECTS:
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "swap",
                        "code": "TOO_MANY_SWAP_OBJECTS",
                        "message": f"sheet has {len(sheet_objects)} objects > {MAX_SWAP_OBJECTS}",
                    }
                    _push_error(
                        errors,
                        "swap",
                        sheet_index=sheet_index,
                        code="TOO_MANY_SWAP_OBJECTS",
                        message=f"{len(sheet_objects)} > {MAX_SWAP_OBJECTS}",
                    )
                    failed += 1
                    return

                # ── (0) Swap call ───────────────────────────────────────
                # One eligible-crop list feeds BOTH compose + cut (slice alignment;
                # prevents blank-final clobber — see _eligible_sheet_crops).
                eligible_crops = _eligible_sheet_crops(sheet)
                try:
                    req = _build_core_request(
                        sheet,
                        sheet_objects,
                        eligible_crops,
                        model=swap_model,
                        temperature=swap_temperature,
                    )
                    core_result = await run_swap_sprite_sheet(req, ai_context=ai_ctx)
                except RemixDomainError as exc:
                    stage = _map_code_to_stage(exc.code)
                    object_key = (exc.details or {}).get("object_key")
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": stage,
                        "code": exc.code,
                        "message": exc.message,
                        **({"object_key": object_key} if object_key else {}),
                    }
                    _push_error(
                        errors,
                        stage,
                        sheet_index=sheet_index,
                        code=exc.code,
                        message=exc.message,
                        object_key=object_key,
                    )
                    failed += 1
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "sheet_swap_unexpected job_id=%s sheet_index=%d",
                        ctx.id, sheet_index,
                    )
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "internal",
                        "message": f"unexpected: {exc}",
                    }
                    _push_error(
                        errors,
                        "internal",
                        sheet_index=sheet_index,
                        message=f"unexpected: {exc}",
                    )
                    failed += 1
                    return

                # Defensive: return_bytes mode MUST populate image_bytes.
                if not core_result.image_bytes:
                    logger.error(
                        "sheet_swap_no_bytes job_id=%s sheet_index=%d",
                        ctx.id, sheet_index,
                    )
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "cut",
                        "code": "CUT_FAILED",
                        "message": "primitive returned no bytes (return_bytes invariant)",
                    }
                    _push_error(
                        errors, "cut", sheet_index=sheet_index,
                        code="CUT_FAILED",
                        message="primitive returned no bytes",
                    )
                    failed += 1
                    return

                # ── ⚡ Upload RAW swapped sheet → media_url (permanent) ──
                # The primitive ran in return_bytes mode (no upload). Persist the
                # raw Gemini-native sheet → swap_results[].media_url (1 upload/
                # sheet → sprite-sheet-swaps/). Upload-fail is sheet-fatal.
                try:
                    raw_sheet_url = await upload_bytes(
                        build_dated_path(STORAGE_SWAP_PREFIX),
                        core_result.image_bytes,
                        content_type="image/png",
                    )
                except StorageUploadError as exc:
                    logger.warning(
                        "raw_sheet_upload_fail job_id=%s sheet_index=%d reason=%s",
                        ctx.id, sheet_index, exc.reason,
                    )
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "persist",
                        "message": "raw sheet upload failed",
                    }
                    _push_error(
                        errors, "persist", sheet_index=sheet_index,
                        message="raw sheet upload failed",
                    )
                    failed += 1
                    return

                # ── Heartbeat #1 (post-swap+upload, pre-cut) ────────────
                if await _emit_heartbeat("swap_done"):
                    sheets_block[key] = "cancelled"
                    skipped += 1
                    return

                # ── ⚡detect-crop-geometry delegate (2026-06-26) ─────────
                # Locate each cell box on the swapped sprite sheet via the
                # in-process detect core. API fail / notFound → static fallback
                # per crop (never fatal). Skipped when Ảnh #1 is unavailable.
                cut_crops = _build_cut_crops(eligible_crops)
                box_by_index: dict[int, tuple[int, int, int, int]] = {}
                if core_result.composed_sheet_url:
                    hints = [_recognition_hint_sprite(c) for c in cut_crops]
                    box_by_index = await detect_boxes_for_cut(
                        core_result.image_bytes,
                        sheet_geometry=sheet.get("sheet_geometry") or {},
                        crops=cut_crops,
                        original_sheet_url=core_result.composed_sheet_url,
                        swapped_sheet_url=raw_sheet_url,
                        recognition_hints=hints,
                        sheet_idx=sheet_index,
                    )

                # ── POST-SWAP PIPELINE: CUT ONLY ────────────────────────
                # cut N pieces at native dim → resize to original cell geometry →
                # upload final. recut crops carry type/object_key/variant_key/
                # geometry (echoed from the cut-crop dicts) + media_url.
                try:
                    recut_crops = await cut_sprite_sheet_to_crops(
                        core_result.image_bytes,
                        sheet.get("sheet_geometry") or {},
                        cut_crops,
                        sheet_idx=sheet_index,
                        box_by_index=box_by_index,
                    )
                except PostSwapPipelineError as exc:
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "cut",
                        "code": exc.code,
                        "message": exc.message,
                    }
                    _push_error(
                        errors, "cut", sheet_index=sheet_index,
                        code=exc.code, message=exc.message,
                    )
                    failed += 1
                    return

                # ── Heartbeat #2 (post-cut, pre-apply) ──────────────────
                if await _emit_heartbeat("swap_done"):
                    sheets_block[key] = "cancelled"
                    skipped += 1
                    return

                # ── APPLY in-memory (this task owns crop_sheets[idx]) ────
                if force_resweep:
                    sheet["swap_results"] = []
                swap_results = sheet.get("swap_results")
                if not isinstance(swap_results, list):
                    swap_results = []
                    sheet["swap_results"] = swap_results
                for r in swap_results:
                    if isinstance(r, dict):
                        r["is_selected"] = False

                # R1 winner mutex: each just-swapped crop claims is_final for its
                # (type, object_key, variant_key); clear is_final on the same key
                # across OTHER sheets of this sprite. Pure in-memory (persisted
                # with the full-blob UPDATE below).
                cleared_total = 0
                for crop in recut_crops:
                    crop["is_final"] = True
                    cleared_total += clear_foreign_finals(
                        sprite,
                        (
                            crop.get("type"),
                            crop.get("object_key"),
                            crop.get("variant_key"),
                        ),
                        except_sheet=sheet_index,
                    )

                swap_results.append(
                    {
                        "media_url": raw_sheet_url,
                        "created_time": _now_iso(),
                        "is_selected": True,
                        "crops": recut_crops,
                    }
                )
                logger.info(
                    "sprite_sheet_done job_id=%s sheet_index=%d crops=%d finals_cleared=%d",
                    ctx.id, sheet_index, len(recut_crops), cleared_total,
                )
                sheets_block[key] = "done"
                swapped += 1
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

    # ── 1 FULL-COLUMN write AFTER gather (single-writer) ─────────────
    if swapped > 0:
        try:
            await adapter.update_remix_columns(UUID(remix_id), {"sprites": sprites})
        except Exception as exc:  # noqa: BLE001
            logger.exception("sprite_persist_failed remix_id=%s", remix_id)
            for i in valid_indices:
                ikey = str(i)
                if sheets_block.get(ikey) == "done":
                    sheets_block[ikey] = {
                        "state": "failed",
                        "stage": "persist",
                        "message": "persist failed",
                    }
                    swapped -= 1
                    failed += 1
                    _push_error(errors, "persist", sheet_index=i, message=str(exc))
                if i in pre_state:
                    crop_sheets[i]["swap_results"] = pre_state[i]
            async with report_lock:
                await ctx.report(current_step=done_count, step_details=step_details)

    # Late-cancel AFTER persist: keep committed work when swapped>0.
    if await ctx.check_cancel() and swapped == 0:
        return (
            "cancelled",
            _build_result(sprite_id, object_count, swapped, skipped, failed, errors),
        )

    logger.info(
        "remix_sprite_swap_done job_id=%s swapped=%d skipped=%d failed=%d errors=%d",
        ctx.id, swapped, skipped, failed, len(errors),
    )
    return (
        "completed",
        _build_result(sprite_id, object_count, swapped, skipped, failed, errors),
    )
