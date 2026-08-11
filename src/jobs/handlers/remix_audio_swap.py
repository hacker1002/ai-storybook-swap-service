"""Handler `remix_audio_swap` — regen dirty TTS chunks + rebuild combined audio
per textbox/language for every spread in scope.

Ported VERBATIM from image-api `src/jobs/handlers/remix_audio_swap.py` (P3b). The
ONLY changes are the I/O seams:
  - `get_supabase_client()` reads → `src.db.adapter.get_adapter()` asyncpg calls
    (`get_remix`, `update_remix_columns`);
  - `build_eleven_id_cache(voice_ids)` reads via the adapter (no `sb` arg — see
    `remix_voice_resolver`);
  - `AiCallContext` threads audit `admin_ref`/`sid` from `job.params` and pins
    `user_id=None` (this service has no user directory).
Step order, EVERY `check_cancel` call-site, the per-spread FIFO stage machine,
`result`/`step_details` structure and `JobError.stage` values are preserved exactly.

Decisions (locked in spec 01-enqueue-remix-audio-swap.md §Decisions):
  - Sequential per spread (1 UPDATE/spread → no JSONB write race).
  - Parallel textbox cap = MAX_CONCURRENT_TEXTBOXES_PER_SPREAD (hardcoded 2).
  - Parallel chunks within textbox = params.max_concurrent_chunks_per_textbox.
  - Chunk fail → skip whole textbox (no partial combine).
  - FIFO stage_first_fail wins for step_details.spreads[id].stage.
  - chunk.reader_key + per-chunk inference params PRESERVED across swap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from src.db.adapter import get_adapter
from src.jobs.runner import JobContext, register
from src.core.job_types import JOB_TYPE_AUDIO_SWAP
from src.services.ai_usage import AiCallContext
from src.models.jobs.remix_audio_swap import (
    MAX_CONCURRENT_TEXTBOXES_PER_SPREAD,
    MAX_RESULT_ERRORS,
)
from src.models.requests.narrate_script import (
    NarrateScriptRequest,
    NarrateScriptSettings,
)
from src.models.requests.text_combine_audio_chunks import CombineAudioChunksRequest
from src.services.narration.combine_audio_chunks_core import run_combine_audio_chunks
from src.services.narration.errors import NarrationDomainError
from src.services.narration.narrate_script_core import run_narrate_script
from src.services.remix_voice_resolver import (
    build_eleven_id_cache,
    iter_enabled_languages,
    needs_regen,
    resolve_voice_for_chunk,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_step_timing(
    step_timings: dict[str, Any],
    spread_id: str,
    started_at: str,
    start_monotonic: float,
    chunks_regenerated: int,
    textboxes_recombined: int,
) -> None:
    """Record per-spread (= per job step) execution timing into `step_details`.

    Kept in step_details (not result): step_details is persisted incrementally
    via `ctx.report`, so timings survive a mid-job crash — exactly the debug case.
    """
    step_timings[spread_id] = {
        "started_at": started_at,
        "duration_ms": round((time.monotonic() - start_monotonic) * 1000),
        "chunks_regenerated": chunks_regenerated,
        "textboxes_recombined": textboxes_recombined,
    }


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
    updated: int,
    failed: int,
    chunks_done: int,
    textboxes_done: int,
    errors: list[dict],
) -> dict[str, Any]:
    return {
        "updated_spreads": updated,
        "failed_spreads": failed,
        "total_chunks_regenerated": chunks_done,
        "total_textboxes_recombined": textboxes_done,
        "errors": errors,
    }


def _find_spread_index(spreads: list[dict], spread_id: str) -> int | None:
    for idx, sp in enumerate(spreads):
        if isinstance(sp, dict) and sp.get("id") == spread_id:
            return idx
    return None


def _collect_unique_voice_ids(
    spreads: list[dict],
    remix_config: dict,
    spread_id_scope: list[str],
    langs: list[str],
) -> set[str]:
    """Gather chunk.voice_id + resolved voice_id across in-scope spreads."""
    scope = set(spread_id_scope)
    voices: set[str] = set()
    for spread in spreads:
        if not isinstance(spread, dict):
            continue
        if spread.get("id") not in scope:
            continue
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
                for chunk in audio.get("chunks") or []:
                    if not isinstance(chunk, dict):
                        continue
                    vid = chunk.get("voice_id")
                    if vid:
                        voices.add(vid)
                    resolved = resolve_voice_for_chunk(chunk, remix_config)
                    if resolved:
                        voices.add(resolved)
    return voices


@register(JOB_TYPE_AUDIO_SWAP)
async def handle(job: dict, ctx: JobContext) -> tuple[str, dict | None]:
    params = job.get("params") or {}
    remix_id: str = params["remix_id"]
    chunks_cap = int(params.get("max_concurrent_chunks_per_textbox", 4))

    # AI-usage attribution (Phase 05): built from the JOB ROW (NOT JobContext). The
    # `remix_id` routes the ElevenLabs TTS cost to the remix billing bucket. NO
    # snapshot_id — winner-stamp is remix→remixId ONLY. Audit `admin_ref`/`sid` are
    # stamped into `params` by the enqueue route; `user_id` is None (no user directory).
    ai_ctx = AiCallContext(
        job_id=job["id"],
        user_id=None,
        book_id=job.get("book_id"),
        remix_id=(job.get("params") or {}).get("remix_id"),
        admin_ref=params.get("admin_ref"),
        sid=params.get("sid"),
    )

    adapter = get_adapter()
    errors: list[dict] = []
    updated = failed = chunks_done = textboxes_done = done_count = 0

    # Load remix.
    try:
        remix = await adapter.get_remix(UUID(remix_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("remix_load_failed remix_id=%s", remix_id)
        _push_error(errors, "internal", message=f"remix load failed: {exc}")
        return ("failed", _build_result(0, 0, 0, 0, errors))

    if not remix:
        _push_error(errors, "internal", message="remix_not_found")
        return ("failed", _build_result(0, 0, 0, 0, errors))

    remix_config = remix.get("remix_config") or {}
    illustration = remix.get("illustration") or {}
    spreads: list[dict] = illustration.get("spreads") or []

    step_details: dict[str, Any] = job.get("step_details") or {}
    spreads_block = step_details.get("spreads")
    if not isinstance(spreads_block, dict):
        spreads_block = {}
        step_details["spreads"] = spreads_block

    step_timings = step_details.get("step_timings")
    if not isinstance(step_timings, dict):
        step_timings = {}
        step_details["step_timings"] = step_timings

    # Defensive: if enqueue init failed, rebuild scope from illustration via precheck.
    if not spreads_block:
        for sp in spreads:
            if not isinstance(sp, dict):
                continue
            for tb in sp.get("textboxes") or []:
                if not isinstance(tb, dict):
                    continue
                for lang in iter_enabled_languages(remix_config):
                    audio = (tb.get(lang) or {}).get("audio") if isinstance(tb.get(lang), dict) else None
                    if not isinstance(audio, dict):
                        continue
                    for c in audio.get("chunks") or []:
                        if isinstance(c, dict) and needs_regen(c, remix_config):
                            spreads_block[sp["id"]] = "pending"
                            break
                    if sp["id"] in spreads_block:
                        break
                if sp["id"] in spreads_block:
                    break

    spreads_in_scope: list[str] = list(spreads_block.keys())
    if not spreads_in_scope:
        return ("completed", _build_result(0, 0, 0, 0, errors))

    langs = iter_enabled_languages(remix_config)
    voice_ids = _collect_unique_voice_ids(spreads, remix_config, spreads_in_scope, langs)
    eleven_cache = await build_eleven_id_cache(voice_ids)

    logger.info(
        "remix_audio_swap_start job_id=%s remix_id=%s spreads=%d chunks_cap=%d langs=%s",
        ctx.id,
        remix_id,
        len(spreads_in_scope),
        chunks_cap,
        ",".join(langs) or "<none>",
    )

    textbox_sem = asyncio.Semaphore(MAX_CONCURRENT_TEXTBOXES_PER_SPREAD)

    for spread_id in spreads_in_scope:
        if await ctx.check_cancel():
            return (
                "cancelled",
                _build_result(updated, failed, chunks_done, textboxes_done, errors),
            )

        spreads_block[spread_id] = "running"
        spread_started_at = _now_iso()
        spread_t0 = time.monotonic()
        chunks_before, textboxes_before = chunks_done, textboxes_done
        await ctx.report(current_step=done_count, step_details=step_details)

        spread_idx = _find_spread_index(spreads, spread_id)
        if spread_idx is None:
            _push_error(
                errors, "internal", spread_id=spread_id, message="spread_not_found"
            )
            spreads_block[spread_id] = {
                "state": "failed",
                "stage": "internal",
                "message": "spread_not_found",
            }
            failed += 1
            _record_step_timing(
                step_timings, spread_id, spread_started_at, spread_t0,
                chunks_done - chunks_before, textboxes_done - textboxes_before,
            )
            done_count += 1
            await ctx.report(current_step=done_count, step_details=step_details)
            continue

        spread = spreads[spread_idx]
        spread_dirty = False
        failed_tbids: list[str] = []
        stage_first_fail: str | None = None

        chunks_sem = asyncio.Semaphore(chunks_cap)

        async def process_textbox_lang(textbox: dict, lang_key: str):
            nonlocal chunks_done, textboxes_done, stage_first_fail, spread_dirty
            async with textbox_sem:
                lang_block = textbox.get(lang_key) if isinstance(textbox, dict) else None
                audio = lang_block.get("audio") if isinstance(lang_block, dict) else None
                if not isinstance(audio, dict):
                    return
                chunks = audio.get("chunks") or []
                if not chunks:
                    return

                dirty: list[tuple[int, dict]] = [
                    (i, c)
                    for i, c in enumerate(chunks)
                    if isinstance(c, dict) and needs_regen(c, remix_config)
                ]
                if not dirty:
                    return

                async def regen_one(idx: int, chunk: dict):
                    async with chunks_sem:
                        resolved = (
                            resolve_voice_for_chunk(chunk, remix_config)
                            or chunk.get("voice_id")
                        )
                        if not resolved:
                            raise NarrationDomainError(
                                status=422,
                                code="VOICE_UNRESOLVED",
                                message=f"chunk[{idx}] has no resolvable voice",
                            )
                        eleven_id = eleven_cache.get(resolved)
                        if not eleven_id:
                            raise NarrationDomainError(
                                status=404,
                                code="VOICE_NOT_FOUND",
                                message=f"voice {resolved} missing eleven_id",
                            )
                        narrate_req = NarrateScriptRequest(
                            script=f"@{eleven_id}: {chunk.get('script', '')}",
                            settings=NarrateScriptSettings(
                                stability=chunk.get("stability", 0.5),
                                similarity_boost=chunk.get(
                                    "similarity", chunk.get("similarity_boost", 0.75)
                                ),
                                style=chunk.get(
                                    "exaggeration", chunk.get("style", 0.0)
                                ),
                                speed=chunk.get("speed", 1.0),
                                seed=chunk.get("seed"),
                            ),
                            model_id="eleven_v3",
                            output_format="mp3_44100_128",
                        )
                        result = await run_narrate_script(narrate_req, ai_context=ai_ctx)
                        return (idx, result, resolved)

                # 1. Parallel narrate
                try:
                    regen_results = await asyncio.gather(
                        *[regen_one(i, c) for (i, c) in dirty]
                    )
                except NarrationDomainError as exc:
                    _push_error(
                        errors,
                        "narrate-script",
                        spread_id=spread_id,
                        textbox_id=textbox.get("id"),
                        language_key=lang_key,
                        message=f"[{exc.code}] {exc.message}",
                    )
                    if textbox.get("id"):
                        failed_tbids.append(textbox["id"])
                    if stage_first_fail is None:
                        stage_first_fail = "narrate-script"
                    return
                except Exception as exc:  # noqa: BLE001
                    _push_error(
                        errors,
                        "narrate-script",
                        spread_id=spread_id,
                        textbox_id=textbox.get("id"),
                        language_key=lang_key,
                        message=str(exc),
                    )
                    if textbox.get("id"):
                        failed_tbids.append(textbox["id"])
                    if stage_first_fail is None:
                        stage_first_fail = "narrate-script"
                    return

                chunks_done += len(regen_results)

                # 2. Apply in-memory mutations.
                now_iso = _now_iso()
                for idx, narrate_result, resolved in regen_results:
                    chunk = chunks[idx]
                    chunk["voice_id"] = resolved
                    chunk["script_synced"] = True
                    # reader_key + per-chunk inference params PRESERVED — do not touch.
                    results = chunk.get("results")
                    if not isinstance(results, list):
                        results = []
                        chunk["results"] = results
                    for r in results:
                        if isinstance(r, dict):
                            r["is_selected"] = False
                    results.append(
                        {
                            "url": narrate_result.audio_url,
                            "word_timings": narrate_result.words,
                            "raw_alignment": narrate_result.raw_alignment,
                            "created_time": now_iso,
                            "is_selected": True,
                        }
                    )

                # 3. Combine — shortcut when len==1, else loopback.
                try:
                    if len(chunks) == 1:
                        sole = chunks[0]
                        selected = next(
                            (
                                r
                                for r in (sole.get("results") or [])
                                if isinstance(r, dict) and r.get("is_selected")
                            ),
                            None,
                        )
                        if not selected:
                            raise RuntimeError("no selected result after regen")
                        audio["combined_audio_url"] = selected.get("url")
                        audio["word_timings"] = selected.get("word_timings") or []
                    else:
                        payload_chunks: list[dict[str, Any]] = []
                        for c in chunks:
                            sel = next(
                                (
                                    r
                                    for r in (c.get("results") or [])
                                    if isinstance(r, dict) and r.get("is_selected")
                                ),
                                None,
                            )
                            if not sel:
                                raise RuntimeError(
                                    f"chunk has no selected result (script={c.get('script', '')[:40]!r})"
                                )
                            payload_chunks.append(
                                {
                                    "audioUrl": sel.get("url"),
                                    "script": c.get("script", ""),
                                    "wordTimings": sel.get("word_timings") or [],
                                }
                            )
                        try:
                            combine_req = CombineAudioChunksRequest.model_validate(
                                {
                                    "chunks": payload_chunks,
                                    "outputFormat": "mp3_44100_128",
                                }
                            )
                        except ValidationError as ve:
                            raise NarrationDomainError(
                                status=400,
                                code="VALIDATION_ERROR",
                                message=str(ve),
                            ) from ve
                        combined = await run_combine_audio_chunks(combine_req)
                        audio["combined_audio_url"] = combined.audio_url
                        audio["word_timings"] = combined.words
                    textboxes_done += 1
                except NarrationDomainError as exc:
                    _push_error(
                        errors,
                        "combine-audio-chunks",
                        spread_id=spread_id,
                        textbox_id=textbox.get("id"),
                        language_key=lang_key,
                        message=f"[{exc.code}] {exc.message}",
                    )
                    if textbox.get("id"):
                        failed_tbids.append(textbox["id"])
                    if stage_first_fail is None:
                        stage_first_fail = "combine-audio-chunks"
                    return
                except Exception as exc:  # noqa: BLE001
                    _push_error(
                        errors,
                        "combine-audio-chunks",
                        spread_id=spread_id,
                        textbox_id=textbox.get("id"),
                        language_key=lang_key,
                        message=str(exc),
                    )
                    if textbox.get("id"):
                        failed_tbids.append(textbox["id"])
                    if stage_first_fail is None:
                        stage_first_fail = "combine-audio-chunks"
                    return

                # 4. Rollup audio.is_sync.
                audio["is_sync"] = all(
                    bool(c.get("script_synced")) and bool(c.get("params_synced"))
                    for c in chunks
                    if isinstance(c, dict)
                )
                spread_dirty = True

        # Build tasks for every (textbox, lang) tuple.
        tasks = []
        for tb in spread.get("textboxes") or []:
            if not isinstance(tb, dict):
                continue
            for lang in langs:
                tasks.append(process_textbox_lang(tb, lang))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

        if spread_dirty:
            try:
                await adapter.update_remix_columns(
                    UUID(remix_id), {"illustration": illustration}
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("spread_persist_failed spread_id=%s", spread_id)
                _push_error(
                    errors,
                    "persist",
                    spread_id=spread_id,
                    message=str(exc),
                )
                spreads_block[spread_id] = {
                    "state": "failed",
                    "stage": "persist",
                    "message": str(exc),
                }
                failed += 1
                _record_step_timing(
                    step_timings, spread_id, spread_started_at, spread_t0,
                    chunks_done - chunks_before, textboxes_done - textboxes_before,
                )
                done_count += 1
                await ctx.report(current_step=done_count, step_details=step_details)
                continue

        if failed_tbids:
            spreads_block[spread_id] = {
                "state": "failed",
                "stage": stage_first_fail or "narrate-script",
                "message": f"{len(failed_tbids)} textbox(es) failed",
                "failed_textbox_ids": list(dict.fromkeys(failed_tbids)),
            }
            failed += 1
        else:
            spreads_block[spread_id] = "done"
            updated += 1

        _record_step_timing(
            step_timings, spread_id, spread_started_at, spread_t0,
            chunks_done - chunks_before, textboxes_done - textboxes_before,
        )
        done_count += 1
        await ctx.report(current_step=done_count, step_details=step_details)
        logger.info(
            "spread_done id=%s updated=%d failed=%d chunks_done=%d textboxes_done=%d",
            spread_id,
            updated,
            failed,
            chunks_done,
            textboxes_done,
        )

    if await ctx.check_cancel():
        return (
            "cancelled",
            _build_result(updated, failed, chunks_done, textboxes_done, errors),
        )

    logger.info(
        "remix_audio_swap_done job_id=%s updated=%d failed=%d errors=%d",
        ctx.id,
        updated,
        failed,
        len(errors),
    )
    return (
        "completed",
        _build_result(updated, failed, chunks_done, textboxes_done, errors),
    )
