"""Handler `remix_mix_swap` — swap every in-scope crop sheet of ONE batch entry
(`remixes.mixes[]`, identified by `id`) using the multi-target AI primitive
`run_swap_mix_sheet`, then a CUT-ONLY post-swap pipeline (⚡rev9 2026-06-12),
writing `swap_results[]` with LEAN `crops[]`.

Ported VERBATIM from image-api `src/jobs/handlers/remix_mix_swap.py` (P3b). The
ONLY changes are the I/O seams:
  - `get_supabase_client()` reads → `src.db.adapter.get_adapter()` asyncpg calls
    (`get_remix`, `get_current_snapshot`, `update_remix_columns`);
  - `AiCallContext` threads audit `admin_ref`/`sid` from `job.params` and pins
    `user_id=None` (this service has no user directory).
Step order, EVERY `check_cancel` call-site, the per-sheet stage machine,
`result`/`step_details` structure and `JobError.stage` values are preserved exactly.

PII: never log/echo URLs, names, or base64. `step_details`/`errors[].message`
carry only codes + concise messages + `target_key` (entity key, non-PII).
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
from src.jobs.helpers.promote_is_final import promote_is_final_for_sheet
from src.jobs.runner import JobContext, register
from src.core.job_types import JOB_TYPE_MIX_SWAP
from src.services.ai_usage import AiCallContext
from src.models.jobs.remix_mix_swap import (
    MAX_CONCURRENT_SHEETS,
    MAX_RESULT_ERRORS,
)
from src.models.requests.swap_mix_crop_sheet import (
    MAX_SWAP_TARGETS,
    Crop,
    Geometry,
    SheetGeometry,
    SwapMixSheetCoreRequest,
    SwapTarget,
)
from src.services.remix.errors import RemixDomainError
from src.services.remix.mix_swap_resolver import (
    find_batch_by_id,
    resolve_mix_swap_context,
    select_sheet_targets,
)
from src.services.remix.detect_crop_geometry_service import detect_boxes_for_cut
from src.services.remix.post_swap_pipeline import (
    PostSwapPipelineError,
    cut_and_upload_native,
)
from src.services.remix.swap_image_helpers import build_dated_path
from src.services.remix.swap_mix_prompt_builder import object_mention
from src.services.remix.swap_mix_sheet_core import (
    STORAGE_SWAP_PREFIX,
    run_swap_mix_sheet,
)
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)

# Maps `RemixDomainError.code` raised by `run_swap_mix_sheet` to the stage
# enum. Post-swap pipeline stages (cut/remove_bg/upscale/crops) are NOT routed
# through here — they raise `PostSwapPipelineError` and the handler reads
# `stage` from the pipeline catch site directly.
_COMPOSE_CODES = {"ALL_CROPS_FAILED"}
_PERSIST_CODES = {"STORAGE_UPLOAD_ERROR"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map_code_to_stage(code: str | None) -> str:
    if code in _COMPOSE_CODES:
        return "compose"
    if code in _PERSIST_CODES:
        return "persist"
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
    batch_id: str,
    target_count: int,
    swapped: int,
    skipped: int,
    failed: int,
    errors: list[dict],
) -> dict[str, Any]:
    # ⚡rev8: `unchanged_count` removed (always 0 since rev4; contract 04 rev6
    # dropped unchanged_references entirely).
    return {
        "batch_id": batch_id,
        "target_count": target_count,
        "swapped_sheets": swapped,
        "skipped_sheets": skipped,
        "failed_sheets": failed,
        "errors": errors,
    }


def _build_swap_targets(target_dicts: list[dict]) -> list[SwapTarget]:
    """Project resolver swap_target dicts into the primitive's `SwapTarget`
    model (`object_context` dict is coerced to `CropSheetCharacterContext`)."""
    return [SwapTarget(**t) for t in target_dicts]


def _build_pipeline_crops(stored_crops: list[Any]) -> list[dict[str, Any]]:
    """Project stored `original_crops[]` into the slim shape the cut reads.

    The ⚡rev9 cut-only pipeline needs `{spread_id?, id, geometry}` per crop —
    anything else (media_url, tags, etc.) is irrelevant to cut+upload (the lean
    output echoes only `spread_id`/`id`). Falls back to a synthetic `crop-{idx}`
    id when the stored crop has none, matching the parity in
    `_build_core_request`.
    """
    out: list[dict[str, Any]] = []
    for idx, c in enumerate(stored_crops):
        if not isinstance(c, dict) or not isinstance(c.get("geometry"), dict):
            continue
        geom = c["geometry"]
        out.append(
            {
                "spread_id": c.get("spread_id"),
                "id": c.get("id") or f"crop-{idx}",
                "geometry": {
                    "x": int(geom.get("x", 0)),
                    "y": int(geom.get("y", 0)),
                    "w": int(geom.get("w", 0)),
                    "h": int(geom.get("h", 0)),
                },
            }
        )
    return out


def _recognition_hint_mix(
    crop: dict[str, Any],
    annotation_map: dict[tuple[str | None, str | None], dict],
) -> str | None:
    """Scene-recognition hint for detect-crop-geometry (mix): the runtime
    annotation `description` keyed `(spread_id, id)`. Disambiguates cells with
    similar composition; None when no annotation (graceful — Gemini still matches
    by scene). PII-safe (never logged)."""
    ann = annotation_map.get((crop.get("spread_id"), crop.get("id")))
    if isinstance(ann, dict):
        desc = ann.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()[:500]
    return None


def _build_annotation_map(
    illustration: dict | None,
) -> dict[tuple[str | None, str | None], dict]:
    """⚡rev9 — annotation resolved at runtime from `illustration` (crops no
    longer clone it — DB lean shape 2026-06-12).

    Keyed `(spread_id, image_id)` to match `original_crops[].{spread_id, id}`
    ((spread_id, id) is invariant across the crop pipeline — it always
    identifies the source illustration layer). Only entries with a non-empty
    `annotation` are kept.
    """
    out: dict[tuple[str | None, str | None], dict] = {}
    for spread in (illustration or {}).get("spreads") or []:
        if not isinstance(spread, dict):
            continue
        spread_id = spread.get("id")
        for img in spread.get("images") or []:
            if not isinstance(img, dict):
                continue
            annotation = img.get("annotation")
            if isinstance(annotation, dict) and annotation:
                out[(spread_id, img.get("id"))] = annotation
    return out


def _set_pipeline_stage_failed(
    sheets_block: dict[str, Any],
    key: str,
    errors: list[dict],
    sheet_index: int,
    *,
    stage: str,
    code: str,
    message: str,
) -> None:
    """DRY: mark a sheet failed at a post-swap pipeline stage + push to errors[]."""
    sheets_block[key] = {
        "state": "failed",
        "stage": stage,
        "code": code,
        "message": message,
    }
    _push_error(
        errors,
        stage,
        sheet_index=sheet_index,
        code=code,
        message=message,
    )
    logger.warning(
        "pipeline_stage_failed sheet_index=%d stage=%s code=%s",
        sheet_index, stage, code,
    )


# Fix 2 (2026-06-11) toggle — per-cell variant-qualified `objects` injection
# into the crop manifest (deterministic cell↔target binding). ON — runs
# together with Fix 1 (locator hint, swap_mix_sheet_core).
INJECT_CROP_OBJECTS = True


def _crop_object_mentions(crop: dict) -> list[str]:
    """Variant-qualified mentions of every object tagged in ONE stored crop.

    Derived from `crops[].tags[]` (same source as the swap lineup) and rendered
    through `object_mention` so each entry is byte-identical to the image-guide
    label inner text. This is the DETERMINISTIC cell↔target binding: a bare
    `@key` in the cell `description` cannot distinguish two variants of the
    same object (both Leela variants mention `@leela`), which mis-bound new
    appearances on 3-target sheets. Deduped + sorted for determinism.
    """
    seen: set[str] = set()
    for tag in crop.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        object_key = (tag.get("object_key") or "").strip()
        if not object_key:
            continue
        seen.add(object_mention(object_key, tag.get("variant_key") or "base"))
    return sorted(seen)


def _build_core_request(
    sheet: dict,
    swap_targets: list[SwapTarget],
    annotation_map: dict[tuple[str | None, str | None], dict],
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> SwapMixSheetCoreRequest:
    """Project a stored mix crop sheet into the multi-target primitive's request.

    `model`/`temperature` come from the resolved `job.params.model_params`
    (public id; None → core defaults → parity).

    Stored crops (mixes[].crop_sheets[].original_crops[] — ⚡rev9 lean rename,
    hard cutover) follow the DB schema shape — geometry/media_url, no required
    `id`. The Crop model requires a non-empty `id` (opaque label for
    partial-failure reporting), so synthesize a stable per-position id when
    absent. ⚡rev9: `annotation` is resolved at runtime from `annotation_map`
    keyed `(spread_id, id)` (crops no longer clone it).

    ⚡Phase 2 conform: the per-cell object roster (variant-qualified mentions
    from the crop's tags) is now a FIRST-CLASS `Crop.objects` field — bundled
    with the crop (Validation S1 Q1), NOT smuggled through the free-form
    annotation. The mix builder renders `crop_manifest[].objects` from it; the
    request validator's `build_crop_manifest` stays roster-free. A map-supplied
    `objects` still wins (caller override).

    ⚡rev8: `unchanged_references` no longer exists on the primitive contract
    (04 rev6) — only objects that need swapping are sent; everything else is
    left untouched implicitly.
    """

    def _annotation_and_roster(c: dict) -> tuple[dict | None, list[str] | None]:
        map_ann = dict(annotation_map.get((c.get("spread_id"), c.get("id"))) or {})
        # A map-supplied `objects` wins (caller override); else derive from tags
        # when injection is on. The roster is returned as a FIRST-CLASS value —
        # stripped from the annotation so it isn't double-rendered.
        map_objects = map_ann.pop("objects", None)
        if isinstance(map_objects, list) and map_objects:
            roster: list[str] | None = list(map_objects)
        elif INJECT_CROP_OBJECTS:
            roster = _crop_object_mentions(c) or None
        else:
            roster = None
        return (map_ann or None, roster)

    geom = sheet.get("sheet_geometry") or {}
    crops = []
    for idx, c in enumerate(sheet.get("original_crops") or []):
        if not isinstance(c, dict) or not isinstance(c.get("geometry"), dict):
            continue
        ann, roster = _annotation_and_roster(c)
        crops.append(
            Crop(
                id=c.get("id") or f"crop-{idx}",
                media_url=c.get("media_url"),
                geometry=Geometry(
                    x=c["geometry"]["x"],
                    y=c["geometry"]["y"],
                    w=c["geometry"]["w"],
                    h=c["geometry"]["h"],
                ),
                annotation=ann,
                objects=roster,
            )
        )
    return SwapMixSheetCoreRequest(
        sheet_geometry=SheetGeometry(width=geom["width"], height=geom["height"]),
        crops=crops,
        swap_targets=swap_targets,
        # ⚡detect-delegate (2026-06-26): the composed crop sheet (Ảnh #1, badged)
        # is needed by detect-crop-geometry as the original-scene reference, so
        # the core uploads it → `composed_sheet_url`.
        return_composed_sheet=True,
        # In-process pipeline: primitive skips its own Storage upload. The
        # handler uploads the raw bytes itself (→ media_url) and the `cut` stage
        # consumes `core_result.image_bytes` directly — avoids the 10 MB fetch
        # cap roundtrip on Gemini-native 4K sheets.
        return_bytes=True,
        model=model,
        temperature=temperature,
    )


@register(JOB_TYPE_MIX_SWAP)
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    batch_id: str = params["batch_id"]
    force_resweep: bool = bool(params.get("force_resweep", False))
    # D2 persist guarantees model_params present; D4 — read directly, NO fallback.
    model_params: dict = params["model_params"]
    swap_model: str = model_params["model"]
    swap_temperature = (model_params.get("params") or {}).get("temperature")

    # AI-usage attribution (Phase 05): built from the JOB ROW (NOT JobContext, which
    # only carries id/total_steps). `remix_id` routes the Gemini swap cost to the
    # remix billing bucket (`ai_cost_by_remix`). Audit `admin_ref`/`sid` are stamped
    # into `params` by the enqueue route; `user_id` is None (no user directory).
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
        # ⚡rev9: + illustration — annotation resolved at runtime keyed
        # (spread_id, id) (crops no longer clone it).
        remix = await adapter.get_remix(UUID(remix_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "resolve", message=f"remix load failed: {exc}")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    if not remix:
        _push_error(errors, "resolve", message="remix_not_found")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    remix_characters: list = remix.get("characters") or []
    remix_props: list = remix.get("props") or []
    remix_sprites: list = remix.get("sprites") or []
    mixes: list = remix.get("mixes") or []
    snapshot_id = remix.get("snapshot_id")
    annotation_map = _build_annotation_map(remix.get("illustration"))

    batch = find_batch_by_id(mixes, batch_id)
    if batch is None:
        _push_error(errors, "resolve", message="batch_not_found")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    # Capture once for O(1) lookup in the per-sheet mutex helper (R1) — avoids
    # `mixes.index(batch)` per sheet in the hot path.
    try:
        batch_idx = mixes.index(batch)
    except ValueError:
        batch_idx = -1

    # ── Load snapshot + resolve shared context ONCE (fresh) ──────────
    try:
        snap_characters: list = []
        snap_props: list = []
        if snapshot_id:
            snap = await adapter.get_current_snapshot(UUID(int=0), UUID(str(snapshot_id)))
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
        # Resolve-fail = whole job failed (context is shared by every sheet).
        if mix_ctx.missing_char_refs:
            raise RemixDomainError(
                status=422,
                code="MISSING_VARIANT_REFERENCE",
                message="character tokens missing a sprite final reference",
                details={"tokens": mix_ctx.missing_char_refs},
            )
        if not mix_ctx.swap_targets:
            raise RemixDomainError(
                status=422, code="NO_SWAP_TARGETS", message="no resolvable swap targets"
            )
        # NOTE: budget (>MAX_SWAP_TARGETS) and target_base N-awareness are now
        # enforced PER SHEET (each sheet swaps only the objects present in it),
        # not batch-wide — see `do_sheet`. A batch with >10 distinct tokens but
        # ≤10 per sheet (⚡rev8 cap) is valid; an oversized sheet fails in isolation.
        target_map = mix_ctx.target_map
        missing_target_base = set(mix_ctx.missing_target_base)
    except RemixDomainError as exc:
        _push_error(errors, "resolve", code=exc.code, message=exc.message)
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve_context_failed remix_id=%s", remix_id)
        _push_error(errors, "resolve", message=f"resolve failed: {exc}")
        return ("failed", _build_result(batch_id, 0, 0, 0, 0, errors))

    # target_count = distinct swappable tokens across the WHOLE batch
    # (informational; per-sheet target count varies).
    target_count = mix_ctx.target_count

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
        # Enqueue init failed — rebuild scope from the mix's sheets.
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
            _build_result(batch_id, target_count, 0, 0, 0, errors),
        )

    logger.info(
        "remix_mix_swap_start job_id=%s remix_id=%s sheets=%d targets=%d force_resweep=%s",
        ctx.id,
        remix_id,
        len(scope_indices),
        target_count,
        force_resweep,
    )

    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(batch_id, target_count, 0, 0, 0, errors),
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

                Keeps `updated_at` fresh so a slow Replicate/Gemini stage does
                not exceed REAPER_STALE_SEC between the only-other report (in
                the `finally` block). Also gives the user cancel responsiveness
                without waiting for the full ~8min pipeline to finish.

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

                # ── Per-sheet target subset: swap ONLY objects present in THIS
                #    sheet's original_crops[].tags[] (not the whole batch lineup). ──
                sheet_target_dicts = select_sheet_targets(target_map, sheet)
                if not sheet_target_dicts:
                    # Sheet has crops but none tagged with a swappable token →
                    # nothing to do (no unchanged refs emitted by design).
                    sheets_block[key] = "skipped"
                    skipped += 1
                    return
                # Per-sheet budget (real Gemini per-call ceiling) — fail this
                # sheet only; siblings proceed.
                if len(sheet_target_dicts) > MAX_SWAP_TARGETS:
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "swap",
                        "code": "TOO_MANY_SWAP_TARGETS",
                        "message": f"sheet has {len(sheet_target_dicts)} targets > {MAX_SWAP_TARGETS}",
                    }
                    _push_error(
                        errors,
                        "swap",
                        sheet_index=sheet_index,
                        code="TOO_MANY_SWAP_TARGETS",
                        message=f"{len(sheet_target_dicts)} > {MAX_SWAP_TARGETS}",
                    )
                    failed += 1
                    return
                # target_base N-aware: locator only needed to disambiguate ≥2
                # figures IN THIS SHEET. Missing base + ≥2 targets → fail sheet.
                if len(sheet_target_dicts) >= 2:
                    sheet_missing_base = [
                        t["key"]
                        for t in sheet_target_dicts
                        if t["key"] in missing_target_base
                    ]
                    if sheet_missing_base:
                        sheets_block[key] = {
                            "state": "failed",
                            "stage": "swap",
                            "code": "MISSING_TARGET_BASE",
                            "message": "missing target_base locator (≥2 targets)",
                        }
                        _push_error(
                            errors,
                            "swap",
                            sheet_index=sheet_index,
                            code="MISSING_TARGET_BASE",
                            message="missing target_base locator",
                            target_key=sheet_missing_base[0],
                        )
                        failed += 1
                        return

                # ── (0) Swap call ───────────────────────────────────────
                try:
                    sheet_targets = _build_swap_targets(sheet_target_dicts)
                    req = _build_core_request(
                        sheet,
                        sheet_targets,
                        annotation_map,
                        model=swap_model,
                        temperature=swap_temperature,
                    )
                    core_result = await run_swap_mix_sheet(req, ai_context=ai_ctx)
                except RemixDomainError as exc:
                    stage = _map_code_to_stage(exc.code)
                    target_key = (exc.details or {}).get("target_key")
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": stage,
                        "code": exc.code,
                        "message": exc.message,
                        **({"target_key": target_key} if target_key else {}),
                    }
                    _push_error(
                        errors,
                        stage,
                        sheet_index=sheet_index,
                        code=exc.code,
                        message=exc.message,
                        target_key=target_key,
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
                        "message": f"unexpected: {type(exc).__name__}",
                    }
                    _push_error(
                        errors,
                        "internal",
                        sheet_index=sheet_index,
                        message=f"unexpected: {type(exc).__name__}",
                    )
                    failed += 1
                    return

                # The handler-side crop dict (DB shape) is the source of truth
                # for cut geometry + spread_id — not the primitive's
                # `SwapTarget`. Build a small dict per crop (id + geometry +
                # spread_id) so the cut does not need the full DB blob.
                sheet_geom_dict = sheet.get("sheet_geometry") or {}
                pipeline_crops = _build_pipeline_crops(
                    sheet.get("original_crops") or []
                )

                # Defensive: the primitive in `return_bytes=True` mode MUST
                # populate `image_bytes` (handler<>core contract). If it doesn't,
                # fail the sheet loud — no URL fallback (fail-loud philosophy).
                if not core_result.image_bytes:
                    logger.error(
                        "sheet_swap_no_bytes job_id=%s sheet_index=%d",
                        ctx.id, sheet_index,
                    )
                    _set_pipeline_stage_failed(
                        sheets_block, key, errors, sheet_index,
                        stage="cut", code="CUT_FAILED",
                        message=(
                            "primitive returned no bytes (return_bytes mode "
                            "invariant violated)"
                        ),
                    )
                    failed += 1
                    return

                # ── ⚡ Upload RAW swapped sheet → media_url (permanent) ──
                # The primitive runs in return_bytes mode (no upload). rev7
                # `swap_results[].media_url` = the raw Gemini-native swapped
                # sheet. 1 upload/sheet → `crop-sheet-swaps/`. Upload-fail is
                # sheet-fatal (no valid media_url) → stage=persist.
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
                # Gemini call can run up to GEMINI_TIMEOUT_S=150s; bump
                # updated_at + honor cancel before the cut (~2s, Pillow only —
                # no further checkpoint needed, ⚡rev9).
                if await _emit_heartbeat("swap_done"):
                    sheets_block[key] = "cancelled"
                    skipped += 1
                    return

                # ── ⚡detect-crop-geometry delegate (2026-06-26) ─────────
                # Locate each cell box on the swapped sheet via the in-process
                # detect core (box from numpy, number from Gemini → catches
                # cell reorder). API fail / notFound → static fallback per crop
                # (never fatal). Skipped when the composed sheet (Ảnh #1) is
                # unavailable.
                box_by_index: dict[int, tuple[int, int, int, int]] = {}
                if core_result.composed_sheet_url:
                    hints = [
                        _recognition_hint_mix(c, annotation_map) for c in pipeline_crops
                    ]
                    box_by_index = await detect_boxes_for_cut(
                        core_result.image_bytes,
                        sheet_geometry=sheet_geom_dict,
                        crops=pipeline_crops,
                        original_sheet_url=core_result.composed_sheet_url,
                        swapped_sheet_url=raw_sheet_url,
                        recognition_hints=hints,
                        sheet_idx=sheet_index,
                    )

                # ── (1) ⚡rev9 CUT ONLY — native pieces, upload verbatim ──
                # Pieces keep the Gemini-native dim (NO resize — resolution is
                # preserved for the rmbg/upscale stage jobs). Lean output
                # `{spread_id, id, media_url}`; single piece upload fail →
                # retry 1 → drop; ALL fail → CUT_FAILED (sheet-fatal).
                try:
                    recut_crops = await cut_and_upload_native(
                        core_result.image_bytes,
                        sheet_geom_dict,
                        pipeline_crops,
                        sheet_idx=sheet_index,
                        box_by_index=box_by_index,
                    )
                except PostSwapPipelineError as exc:
                    _set_pipeline_stage_failed(
                        sheets_block, key, errors, sheet_index,
                        stage="cut", code=exc.code, message=exc.message,
                    )
                    failed += 1
                    return

                # ── (2) APPLY in-memory (this task owns crop_sheets[idx]) ─
                # ⚡rev9 shape: media_url = RAW swapped sheet (uploaded above),
                # crops[] = LEAN native pieces {spread_id, id, media_url} —
                # geometry/tags joined from original_crops[] by the reader.
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
                        "media_url": raw_sheet_url,
                        "created_time": _now_iso(),
                        "is_selected": True,
                        "crops": recut_crops,
                    }
                )
                # ── R1 cross-batch `is_final` mutex (2026-05-29) ────────
                # Just-appended crops claim ownership for their `(spread_id,
                # id)` positions; clear `is_final` on the same keys across
                # OTHER batches. Pure in-memory mutation; persisted with the
                # full-blob `UPDATE remixes` step below. Idempotent — re-swap
                # of the same sheet (R2) re-fires this safely.
                promote_stats = promote_is_final_for_sheet(
                    mixes, batch_idx, sheet_index
                )
                logger.info(
                    "is_final_promote ok job_id=%s batch_idx=%d sheet_index=%d "
                    "promoted=%d cleared=%d affected_batches=%d",
                    ctx.id,
                    batch_idx,
                    sheet_index,
                    promote_stats["promoted_count"],
                    promote_stats["cleared_count"],
                    len(promote_stats["affected_batches"]),
                )
                # ⚡rev9: plain "done" — the rev7 per-crop graceful-skip counts
                # (remove_bg/upscale) moved with their stages to jobs 09/10.
                sheets_block[key] = "done"
                swapped += 1
            except Exception as exc:  # noqa: BLE001 — never bubbles to gather
                # Top-level per-sheet guard (parity jobs 09/10): a corrupt
                # sheet shape outside the stage-specific try blocks fails ONLY
                # this sheet, not the whole job (gather has no
                # return_exceptions — an escape would abort sibling sheets).
                logger.exception(
                    "mix_sheet_unexpected job_id=%s sheet_index=%d",
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

    # ── 1 FULL-COLUMN write AFTER gather (single-writer) ─────────────
    if swapped > 0:
        try:
            await adapter.update_remix_columns(UUID(remix_id), {"mixes": mixes})
        except Exception as exc:  # noqa: BLE001
            logger.exception("mix_persist_failed remix_id=%s", remix_id)
            for i in valid_indices:
                key = str(i)
                if sheets_block.get(key) == "done":
                    sheets_block[key] = {
                        "state": "failed",
                        "stage": "persist",
                        "message": "persist failed",
                    }
                    swapped -= 1
                    failed += 1
                    _push_error(
                        errors, "persist", sheet_index=i,
                        message=f"persist failed: {type(exc).__name__}",
                    )
                if i in pre_state:
                    crop_sheets[i]["swap_results"] = pre_state[i]
            async with report_lock:
                await ctx.report(current_step=done_count, step_details=step_details)

    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(
                batch_id, target_count, swapped, skipped, failed, errors
            ),
        )

    logger.info(
        "remix_mix_swap_done job_id=%s swapped=%d skipped=%d failed=%d errors=%d",
        ctx.id,
        swapped,
        skipped,
        failed,
        len(errors),
    )
    return (
        "completed",
        _build_result(
            batch_id, target_count, swapped, skipped, failed, errors
        ),
    )
