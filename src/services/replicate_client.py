"""Replicate client wrappers for SAM3 + Qwen-Layered + LangSmith tracing.

P3b PORT NOTE (Phase 05 re-couple): image-api's version of this module is ALSO the
ADR-050 Replicate choke point — every wrapper writes one `ai_service_logs` row
(success/error) via `src.services.ai_usage`. Phase 02 ported it WITHOUT that logging;
now that `src.services.ai_usage` exists, the choke-point logging is RE-COUPLED here.
TWO forced divergences from image-api (this service has no content-addressed Storage
lib): (1) the raw Replicate OUTPUT URL(s) are recorded as `output_blobs` URL metadata
by the logger (`build_ref_metadata` → `{url}`), NOT fetched+re-hosted to
`ai-logs/outputs/`; so `ReplicatePredictionResult.output_files` stays `()`. (2)
`AiLogEntry` has no `id` column here (the DB mints `ai_service_logs.id`), so entries
never pass `id=`; `new_request_id()` is only a correlation id surfaced as
`data.aiRequestId`. Everything else — official-vs-community dispatch, semaphore,
`create_with_429_retry`, `@traceable`, error taxonomy — is VERBATIM. Logging is
fire-and-forget and NEVER raises into the call (`log_ai_request` swallows).

NOTE: none of the 7 `/api/remix/*` routes call Replicate (all Gemini/Pillow); this
choke point serves the job pipeline (rmbg/upscale) ported in a later phase.
"""

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar
from urllib.parse import urlparse

import replicate
from fastapi import HTTPException
from langsmith import traceable

from src.config.settings import settings
from src.services.ai_usage import (
    AiCallContext,
    AiLogEntry,
    compute_cost,
    extract_ref_blobs,
    log_ai_request,
    new_request_id,
    sanitize_request,
    sanitize_response,
)
from src.models.requests.image_remove_bg import BRIA_REMOVE_BG_MODEL
from src.models.requests.layering_image import QWEN_LAYERED_MODEL
from src.models.requests.remove_text_image import TEXT_REMOVAL_DEFAULT_MODEL
from src.models.requests.normalize_human import (
    FACE_TO_MANY_MODEL,
    FACE_TO_MANY_VERSION,
)
from src.models.requests.segment_layer import SAM3_FIXED_INPUT

logger = logging.getLogger(__name__)

# Public contract. Neutral helpers (`get_replicate_client`,
# `create_with_429_retry`, `_extract_url`, `_is_fetch_error`,
# `_FETCH_ERROR_KEYWORDS`, `_host`) are exported so the image domain core
# (`services/image/upscale_core.py`) can import-share them — keeps the 429
# retry / URL-extraction / fetch-error classification logic DRY without copying
# and without a circular import (this module does NOT import the image domain).
__all__ = [
    "SAM3_MODEL_VERSION",
    "REPLICATE_GLOBAL_INFLIGHT",
    "ReplicatePredictionResult",
    "get_replicate_client",
    "create_with_429_retry",
    "replicate_prediction_slot",
    "run_sam3_segment",
    "run_layering",
    "run_remove_bg",
    "run_remove_text",
    "run_face_to_many",
    "_extract_url",
    "_extract_urls_list",
    "_is_fetch_error",
    "_is_no_face_error",
    "_FETCH_ERROR_KEYWORDS",
    "_host",
]


@dataclass(frozen=True)
class ReplicatePredictionResult:
    """Shared return shape for every Replicate wrapper in this module.

    Exposes the cost-relevant prediction metadata (`prediction_id` +
    `predict_time`) alongside the wrapper's output so a future logging hook
    (Phase 03) can bill per prediction without a second Replicate round-trip.

    `output` keeps each wrapper's own shape — a single URL (`str`) for
    remove-bg / remove-text / face-to-many / SAM3, or a list of URLs
    (`list[str]`) for layering. `predict_time` is `(prediction.metrics or
    {}).get("predict_time")` — `None` when the prediction never ran (early
    failure) or the SDK omitted metrics.
    """

    output: str | list[str]
    prediction_id: str
    predict_time: float | None
    # Correlation id (uuid4) of this call — routers surface it as `data.aiRequestId`.
    # NOT the row id (the DB mints `ai_service_logs.id`). Populated at the choke.
    ai_request_id: str = ""
    # Always () in this service: raw output URL(s) are recorded as `{url}` metadata
    # inside the logger (no content-addressed re-hosting lib here).
    output_files: tuple = ()


def _extract_predict_time(prediction) -> float | None:
    """Best-effort `metrics.predict_time` from a Replicate prediction object.

    Guards a `None`/non-dict `metrics` (early-failed predictions expose no
    metrics) per the phase-04 contract.
    """
    metrics = getattr(prediction, "metrics", None) or {}
    if isinstance(metrics, dict):
        value = metrics.get("predict_time")
        if isinstance(value, (int, float)):
            return float(value)
    return None


# ─── Global Replicate in-flight bound (2026-06-12, plan 260612-1438 OQ#1) ────
# Process-level semaphore wrapping every prediction submit+poll. Per-job
# constants (`MAX_CONCURRENT_SHEETS` × crop caps = 1) only bound WITHIN one job
# type — the rmbg (09) and upscale (10) stage jobs dedup per-type and can run
# in parallel, both hitting Replicate; a dev account allows 1 in-flight call.
# Scope discipline (deadlock guard): wrap ONLY the leaf prediction
# create+wait — NEVER a whole handler or anything that itself acquires the
# slot. Raising the account tier → bump this constant.
REPLICATE_GLOBAL_INFLIGHT: int = 1
_REPLICATE_SEM = asyncio.Semaphore(REPLICATE_GLOBAL_INFLIGHT)


@asynccontextmanager
async def replicate_prediction_slot():
    """Acquire the global Replicate prediction slot (submit+poll section only)."""
    async with _REPLICATE_SEM:
        yield

SAM3_MODEL_VERSION: str = (
    "mattsays/sam3-image:d73db077226443ba4fafd34e233b3626b552eac2a433f90c7c32a9ac89bd9e72"
)

# Keywords (lowercase) indicating Replicate failed to fetch the input image.
# Match against prediction.error → map to HTTP 422 IMAGE_FETCH_ERROR.
_FETCH_ERROR_KEYWORDS: tuple[str, ...] = (
    "fetch",
    "download",
    "invalid image",
    "404",
    "not found",
    "unsupported",
    "content-type",
    "could not load",
)


@lru_cache(maxsize=1)
def get_replicate_client() -> replicate.Client:
    if not settings.replicate_api_token:
        raise RuntimeError("REPLICATE_API_TOKEN is not configured")
    return replicate.Client(api_token=settings.replicate_api_token)


# --- 429 retry for prediction CREATE (POST) -------------------------------
# Replicate SDK's RetryTransport only auto-retries idempotent verbs
# (GET/PUT/DELETE/...). `predictions.async_create` is a POST → NOT retried by
# the SDK. Low-credit accounts (no payment method) are throttled to 1 req/s /
# 6 req/min, so back-to-back creates surface ReplicateError(status=429) with
# detail "Request was throttled. Your rate limit resets in ~30s.".
# We parse that window (+1s buffer), sleep, retry ONCE.
_T = TypeVar("_T")
_THROTTLE_RESET_RE = re.compile(r"~?\s*(\d+(?:\.\d+)?)\s*s\b", re.IGNORECASE)
_RETRY_BUFFER_S: float = 1.0
_RETRY_DEFAULT_WAIT_S: float = 30.0  # fallback when detail has no parseable window
_RETRY_MAX_WAIT_S: float = 65.0  # hard cap so a handler can't hang indefinitely


def _parse_throttle_wait_s(detail: str | None) -> float:
    if detail:
        m = _THROTTLE_RESET_RE.search(detail)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return _RETRY_DEFAULT_WAIT_S


_RETRY_JITTER_FRAC: float = 0.2  # ±20% jitter to desync concurrent retries


async def create_with_429_retry(
    create: Callable[[], Awaitable[_T]],
    *,
    label: str,
    max_attempts: int = 2,
) -> _T:
    """Run a prediction-create coroutine with bounded 429 retry.

    `create` must be a zero-arg factory returning a *fresh* coroutine each call
    (a coroutine can only be awaited once). `max_attempts` is the TOTAL attempt
    budget including the original call — default 2 (= 1 retry, backward-compat
    with all single-call sites). Tile-mode callers bump this so N concurrent
    tile creates can ride out short rate-limit windows on free-tier Replicate
    plans (1 req/s / 6 req/min).

    Behaviour:
      - Non-429 errors → re-raise immediately (caller's taxonomy handles them).
      - 429 → sleep `parsed_window + buffer ± jitter`, retry. After
        `max_attempts` attempts, propagate the LAST 429 unchanged so the
        caller's existing `REPLICATE_RATE_LIMIT` mapping still applies.

    The jitter (±20%) is essential when N tiles all hit the same Replicate
    rate-limit window simultaneously — synchronous retries would collide on
    the next window too. Random offsets spread them across ~6s.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be ≥ 1")

    last_exc: replicate.exceptions.ReplicateError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await create()
        except replicate.exceptions.ReplicateError as exc:
            if getattr(exc, "status", None) != 429:
                raise
            last_exc = exc
            if attempt >= max_attempts:
                break
            base = _parse_throttle_wait_s(getattr(exc, "detail", None))
            jitter = base * _RETRY_JITTER_FRAC * (random.random() * 2 - 1)
            wait_s = max(1.0, min(base + _RETRY_BUFFER_S + jitter, _RETRY_MAX_WAIT_S))
            logger.warning(
                "replicate_429_retry label=%s attempt=%d/%d wait_s=%.1f detail=%s",
                label,
                attempt,
                max_attempts,
                wait_s,
                getattr(exc, "detail", None),
            )
            await asyncio.sleep(wait_s)

    assert last_exc is not None  # loop only exits this way via break after 429
    raise last_exc


def _extract_url(output) -> str | None:
    if output is None:
        return None
    if isinstance(output, str):
        return output or None
    if isinstance(output, list):
        for item in output:
            url = _extract_url(item)
            if url:
                return url
        return None
    url_attr = getattr(output, "url", None)
    if isinstance(url_attr, str):
        return url_attr or None
    if callable(url_attr):
        try:
            return url_attr() or None
        except Exception:
            return None
    return None


def _log_replicate_call(
    *,
    ctx: AiCallContext,
    operation: str,
    model: str,
    prediction,
    inputs: dict,
    status: str,
    output=None,
    error=None,
    num_outputs: int = 1,
    output_urls: list[str] | None = None,
) -> None:
    """Fire-and-forget one `ai_service_logs` row for a Replicate call (ADR-050).

    Called ONLY after a prediction was created (a 429 create-throttle logs nothing).
    `usage_unit` is ALWAYS 'seconds' (`predict_time`). Cost is billed per_output on
    success only. Raw output URL(s) → `output_blobs` (recorded as `{url}` metadata by
    the logger — no re-host, the swap-service divergence). NO `id=` (DB mints it)."""
    ptime = _extract_predict_time(prediction) if prediction is not None else None
    pid = (getattr(prediction, "id", "") or None) if prediction is not None else None
    cost = (
        compute_cost("replicate", model, {"seconds": ptime, "num_outputs": num_outputs})
        if status == "success"
        else None
    )
    log_ai_request(
        AiLogEntry(
            provider="replicate", operation=operation, model=model,
            status=status, context=ctx, provider_request_id=pid,
            usage_unit="seconds", usage_amount=ptime, cost=cost,
            request=sanitize_request(inputs),
            response=(sanitize_response({"output": output}) if output is not None else None),
            error=(str(error)[:2000] if error is not None else None),
            ref_blobs=extract_ref_blobs(inputs),  # input image URL(s) → ref_files
            output_blobs=tuple(output_urls or ()),  # raw output URL(s) → output_files
        )
    )


@traceable(name="sam3_image_segment", run_type="llm")
async def run_sam3_segment(
    image_url: str,
    prompt: str,
    threshold: float,
    timeout_s: float = 60.0,
    *,
    ai_context: AiCallContext | None = None,
) -> ReplicatePredictionResult:
    """Run SAM3 segmentation. Returns ReplicatePredictionResult (output=mask URL).

    Async-native via the predictions API (parity with `run_layering` /
    `run_remove_bg`) so the prediction object — and thus `metrics.predict_time`
    — is captured for the Phase-03 cost hook. `SAM3_MODEL_VERSION` is a *community*
    versioned model (`owner/name:<hash>`), so dispatch MUST use `version=<hash>`
    (the `model=owner/name` endpoint 404s for community models).

    Error taxonomy is byte-parity with the prior sync `client.run` impl:
    timeout → 504 TIMEOUT, any ReplicateError/other failure → 502
    REPLICATE_ERROR, empty output → 502 REPLICATE_ERROR. No 429/fetch split is
    introduced (kept intentionally minimal — a 429 surfaces as 502 as before).
    """
    client = get_replicate_client()
    version = SAM3_MODEL_VERSION.split(":", 1)[1]
    model = SAM3_MODEL_VERSION.split(":", 1)[0]
    payload = {
        "image": image_url,
        "prompt": prompt,
        "threshold": threshold,
        **SAM3_FIXED_INPUT,
    }
    logger.debug(
        "sam3_invoke prompt=%s threshold=%s",
        prompt[:100],
        threshold,
    )

    ctx = ai_context or AiCallContext()
    rid = new_request_id()
    prediction = None
    try:
        try:
            async with replicate_prediction_slot():
                prediction = await create_with_429_retry(
                    lambda: client.predictions.async_create(
                        version=version,
                        input=payload,
                    ),
                    label="sam3",
                )
                await asyncio.wait_for(prediction.async_wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning("sam3_timeout threshold=%s", threshold)
            raise HTTPException(
                status_code=504,
                detail={"success": False, "error": {"code": "TIMEOUT", "message": "SAM3 segmentation timed out"}},
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("sam3_error err=%s", exc)
            raise HTTPException(
                status_code=502,
                detail={"success": False, "error": {"code": "REPLICATE_ERROR", "message": str(exc)}},
            ) from exc

        mask_url = _extract_url(prediction.output)
        if not mask_url:
            logger.error("sam3_empty_output type=%s", type(prediction.output).__name__)
            raise HTTPException(
                status_code=502,
                detail={"success": False, "error": {"code": "REPLICATE_ERROR", "message": "SAM3 returned empty output"}},
            )
        prediction_id = getattr(prediction, "id", "") or ""
        predict_time = _extract_predict_time(prediction)
        _log_replicate_call(
            ctx=ctx, operation="sam3_image_segment", model=model,
            prediction=prediction, inputs=payload, status="success",
            output=mask_url, output_urls=[mask_url],
        )
        return ReplicatePredictionResult(
            output=mask_url,
            prediction_id=prediction_id,
            predict_time=predict_time,
            ai_request_id=rid,
        )
    except HTTPException as exc:
        # Prediction ran then failed → error row. 429 create-throttle keeps
        # `prediction is None` → no row (no billing occurred).
        if prediction is not None:
            _log_replicate_call(
                ctx=ctx, operation="sam3_image_segment", model=model,
                prediction=prediction, inputs=payload, status="error", error=exc.detail,
            )
        raise


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"success": False, "error": {"code": code, "message": message}},
    )


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


def _is_fetch_error(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    low = error_msg.lower()
    return any(k in low for k in _FETCH_ERROR_KEYWORDS)


# Keywords (lowercase) indicating fofr/face-to-many could not detect a face in
# the input portrait. Match against prediction.error → HTTP 422 NO_FACE_DETECTED.
_NO_FACE_KEYWORDS: tuple[str, ...] = (
    "no face",
    "face detect",
    "no person",
    "face not found",
    "no faces",
)


def _is_no_face_error(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    low = error_msg.lower()
    return any(k in low for k in _NO_FACE_KEYWORDS)


# Keywords (lowercase) indicating a content-moderation / safety rejection from a
# Replicate model (e.g. FLUX.1 Kontext text-removal flags sensitive input).
# Match against prediction.error → HTTP 422 SAFETY_FILTER_BLOCKED so callers can
# tell "blocked, retry is futile" apart from a transient REPLICATE_ERROR (502).
# Disjoint from `_FETCH_ERROR_KEYWORDS` (no overlap) so the ordering is safe.
_SAFETY_KEYWORDS: tuple[str, ...] = (
    "nsfw",
    "sensitive",
    "flagged",
    "safety",
    "content moderation",
    "content policy",
    "moderation",
)


def _is_safety_error(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    low = error_msg.lower()
    return any(k in low for k in _SAFETY_KEYWORDS)


def _extract_urls_list(output) -> list[str]:
    if not output:
        return []
    items = output if isinstance(output, list) else [output]
    urls: list[str] = []
    for item in items:
        if isinstance(item, str):
            if item:
                urls.append(item)
            continue
        url_attr = getattr(item, "url", None)
        if isinstance(url_attr, str):
            if url_attr:
                urls.append(url_attr)
        elif callable(url_attr):
            try:
                val = url_attr()
                if isinstance(val, str) and val:
                    urls.append(val)
            except Exception:
                continue
    return urls


@traceable(name="retouch.layering_image.replicate", run_type="llm")
async def run_layering(
    payload: dict,
    timeout_s: float = 300.0,
    *,
    ai_context: AiCallContext | None = None,
) -> ReplicatePredictionResult:
    """Run Qwen image-layered model. Returns ReplicatePredictionResult (output=urls).

    Async-native: uses `predictions.async_create` + `async_wait` so the
    prediction ID can be captured for `meta.replicatePredictionId`.

    Raises HTTPException on any non-success path per spec error table.
    """
    client = get_replicate_client()

    image_host = _host(str(payload.get("image", "")))
    logger.debug(
        "layering_invoke host=%s num_layers=%s output_format=%s go_fast=%s",
        image_host,
        payload.get("num_layers"),
        payload.get("output_format"),
        payload.get("go_fast"),
    )

    ctx = ai_context or AiCallContext()
    rid = new_request_id()
    prediction = None
    try:
        try:
            async with replicate_prediction_slot():
                prediction = await create_with_429_retry(
                    lambda: client.predictions.async_create(
                        model=QWEN_LAYERED_MODEL,
                        input=payload,
                    ),
                    label="layering",
                )
                await asyncio.wait_for(prediction.async_wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning("layering_timeout host=%s timeout_s=%s", image_host, timeout_s)
            raise _error(504, "TIMEOUT", "Layering timed out") from exc
        except HTTPException:
            raise
        except replicate.exceptions.ReplicateError as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("layering_rate_limited host=%s", image_host)
                raise _error(429, "REPLICATE_RATE_LIMIT", "Replicate rate limited") from exc
            logger.error("layering_replicate_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc
        except Exception as exc:
            logger.error("layering_unexpected_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc

        if prediction.status != "succeeded":
            err_msg = prediction.error or "prediction failed"
            if _is_fetch_error(prediction.error):
                logger.warning(
                    "layering_fetch_error host=%s status=%s err=%s",
                    image_host,
                    prediction.status,
                    err_msg,
                )
                raise _error(422, "IMAGE_FETCH_ERROR", str(err_msg))
            logger.error(
                "layering_non_succeeded host=%s status=%s err=%s",
                image_host,
                prediction.status,
                err_msg,
            )
            raise _error(502, "REPLICATE_ERROR", str(err_msg))

        urls = _extract_urls_list(prediction.output)
        if not urls:
            logger.error(
                "layering_empty_output host=%s type=%s",
                image_host,
                type(prediction.output).__name__,
            )
            raise _error(502, "REPLICATE_ERROR", "empty output")

        prediction_id = getattr(prediction, "id", "") or ""
        predict_time = _extract_predict_time(prediction)
        logger.debug(
            "layering_done host=%s num_urls=%d prediction_id=%s",
            image_host,
            len(urls),
            prediction_id[:10],
        )
        _log_replicate_call(
            ctx=ctx, operation="retouch.layering_image.replicate", model=QWEN_LAYERED_MODEL,
            prediction=prediction, inputs=payload, status="success",
            output=urls, output_urls=urls, num_outputs=len(urls),
        )
        return ReplicatePredictionResult(
            output=urls,
            prediction_id=prediction_id,
            predict_time=predict_time,
            ai_request_id=rid,
        )
    except HTTPException as exc:
        if prediction is not None:
            _log_replicate_call(
                ctx=ctx, operation="retouch.layering_image.replicate", model=QWEN_LAYERED_MODEL,
                prediction=prediction, inputs=payload, status="error", error=exc.detail,
            )
        raise


@traceable(name="retouch.image_remove_bg.replicate", run_type="llm")
async def run_remove_bg(
    payload: dict,
    *,
    model: str | None = None,
    version: str | None = None,
    timeout_s: float = 120.0,
    ai_context: AiCallContext | None = None,
    operation: str | None = None,
) -> ReplicatePredictionResult:
    """Run a Replicate remove-background model. Returns ReplicatePredictionResult (output=url).

    `model` is the owner/name ref (public == provider). None → `BRIA_REMOVE_BG_MODEL`
    default (parity). v1 ships `bria/remove-background` (default) +
    `851-labs/background-remover`.

    `operation` (optional) overrides the `ai_service_logs.operation` tag. None →
    `"retouch.image_remove_bg.replicate"` verbatim, so manual-retouch + remix_rmbg
    callers are unchanged; the actor-rmbg job passes `"actor.rmbg"` to bill the
    actor remove-bg cost bucket (parity with `run_swap_mix_sheet(run_name=)` and
    `run_upscale(operation=)`). (P3b Phase 02: the tag is accepted but only used
    once Phase 03 wires the choke-point logging.)

    Two dispatch modes (the rmbg adapter supplies `version` per model):
      - `version` None → OFFICIAL model: `predictions.async_create(model=...)`,
        Replicate resolves the latest version server-side (Bria).
      - `version` set  → COMMUNITY model: `predictions.async_create(version=...)`.
        The `model=owner/name` endpoint 404s for community models (e.g. 851-labs),
        so a pinned version is mandatory.

    Async-native: uses `predictions.async_create` + `async_wait` to capture
    prediction ID for `meta.replicatePredictionId`.

    Raises HTTPException on any non-success path per spec error table.
    """
    client = get_replicate_client()
    resolved_model = model or BRIA_REMOVE_BG_MODEL
    effective_operation = operation or "retouch.image_remove_bg.replicate"

    # Payload key is `image` (the format:uri field). Falls back to legacy
    # `image_url` for any caller still passing that key — log-only, the actual
    # Replicate dispatch uses whatever the caller put in `payload`.
    image_host = _host(str(payload.get("image") or payload.get("image_url") or ""))
    logger.debug(
        "remove_bg_invoke host=%s model=%s version=%s preserve_alpha=%s",
        image_host,
        resolved_model,
        (version[:12] if version else None),
        payload.get("preserve_alpha"),
    )

    def _dispatch():
        # `version is not None` (not truthiness): an explicit version="" should
        # surface a hard failure, not silently fall back to model= dispatch.
        if version is not None:
            return client.predictions.async_create(version=version, input=payload)
        return client.predictions.async_create(model=resolved_model, input=payload)

    ctx = ai_context or AiCallContext()
    rid = new_request_id()
    prediction = None
    try:
        try:
            async with replicate_prediction_slot():
                prediction = await create_with_429_retry(
                    _dispatch,
                    label="remove_bg",
                )
                await asyncio.wait_for(prediction.async_wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning("remove_bg_timeout host=%s timeout_s=%s", image_host, timeout_s)
            raise _error(504, "TIMEOUT", "Remove-bg timed out") from exc
        except HTTPException:
            raise
        except replicate.exceptions.ReplicateError as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("remove_bg_rate_limited host=%s", image_host)
                raise _error(429, "REPLICATE_RATE_LIMIT", "Replicate rate limited") from exc
            logger.error("remove_bg_replicate_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc
        except Exception as exc:
            logger.error("remove_bg_unexpected_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc

        if prediction.status != "succeeded":
            err_msg = prediction.error or "prediction failed"
            if _is_fetch_error(prediction.error):
                logger.warning(
                    "remove_bg_fetch_error host=%s status=%s err=%s",
                    image_host,
                    prediction.status,
                    err_msg,
                )
                raise _error(422, "IMAGE_FETCH_ERROR", str(err_msg))
            logger.error(
                "remove_bg_non_succeeded host=%s status=%s err=%s",
                image_host,
                prediction.status,
                err_msg,
            )
            raise _error(502, "REPLICATE_ERROR", str(err_msg))

        output_url = _extract_url(prediction.output)
        if not output_url:
            logger.error(
                "remove_bg_empty_output host=%s type=%s",
                image_host,
                type(prediction.output).__name__,
            )
            raise _error(502, "REPLICATE_ERROR", "empty output")

        prediction_id = getattr(prediction, "id", "") or ""
        predict_time = _extract_predict_time(prediction)
        logger.debug(
            "remove_bg_done host=%s prediction_id=%s",
            image_host,
            prediction_id[:10],
        )
        _log_replicate_call(
            ctx=ctx, operation=effective_operation, model=resolved_model,
            prediction=prediction, inputs=payload, status="success",
            output=output_url, output_urls=[output_url],
        )
        return ReplicatePredictionResult(
            output=output_url,
            prediction_id=prediction_id,
            predict_time=predict_time,
            ai_request_id=rid,
        )
    except HTTPException as exc:
        if prediction is not None:
            _log_replicate_call(
                ctx=ctx, operation=effective_operation, model=resolved_model,
                prediction=prediction, inputs=payload, status="error", error=exc.detail,
            )
        raise


@traceable(name="retouch.remove_text_image.replicate", run_type="llm")
async def run_remove_text(
    payload: dict,
    *,
    model: str | None = None,
    timeout_s: float = 120.0,
    ai_context: AiCallContext | None = None,
) -> ReplicatePredictionResult:
    """Run Replicate FLUX.1 Kontext text-removal. Returns ReplicatePredictionResult (output=url).

    `model` is the owner/name ref (public == provider). None →
    `TEXT_REMOVAL_DEFAULT_MODEL` (`flux-kontext-apps/text-removal`). OFFICIAL
    model → dispatched by `model=owner/name` (Replicate resolves the latest
    version server-side; no version pin, parity `run_remove_bg` OFFICIAL path).

    Payload key is `input_image` (FLUX.1 Kontext input, format:uri — accepts
    HTTPS URLs AND data URIs). Fixed knobs (`aspect_ratio="match_input_image"`,
    `output_format="png"`, `safety_tolerance`) are owned by the handler, not here.

    Async-native: `predictions.async_create` + `async_wait` to capture the
    prediction ID for `meta.replicatePredictionId`.

    Raises HTTPException on any non-success path per spec error table.
    """
    client = get_replicate_client()
    resolved_model = model or TEXT_REMOVAL_DEFAULT_MODEL

    image_host = _host(str(payload.get("input_image", "")))
    logger.debug(
        "remove_text_invoke host=%s model=%s aspect_ratio=%s",
        image_host,
        resolved_model,
        payload.get("aspect_ratio"),
    )

    ctx = ai_context or AiCallContext()
    rid = new_request_id()
    prediction = None
    try:
        try:
            async with replicate_prediction_slot():
                prediction = await create_with_429_retry(
                    lambda: client.predictions.async_create(
                        model=resolved_model,
                        input=payload,
                    ),
                    label="remove_text",
                )
                await asyncio.wait_for(prediction.async_wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning("remove_text_timeout host=%s timeout_s=%s", image_host, timeout_s)
            raise _error(504, "TIMEOUT", "Text removal timed out") from exc
        except HTTPException:
            raise
        except replicate.exceptions.ReplicateError as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("remove_text_rate_limited host=%s", image_host)
                raise _error(429, "REPLICATE_RATE_LIMIT", "Replicate rate limited") from exc
            logger.error("remove_text_replicate_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc
        except Exception as exc:
            logger.error("remove_text_unexpected_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc

        if prediction.status != "succeeded":
            err_msg = prediction.error or "prediction failed"
            if _is_safety_error(prediction.error):
                logger.warning(
                    "remove_text_safety_blocked host=%s status=%s err=%s",
                    image_host,
                    prediction.status,
                    err_msg,
                )
                raise _error(422, "SAFETY_FILTER_BLOCKED", str(err_msg))
            if _is_fetch_error(prediction.error):
                logger.warning(
                    "remove_text_fetch_error host=%s status=%s err=%s",
                    image_host,
                    prediction.status,
                    err_msg,
                )
                raise _error(422, "IMAGE_FETCH_ERROR", str(err_msg))
            logger.error(
                "remove_text_non_succeeded host=%s status=%s err=%s",
                image_host,
                prediction.status,
                err_msg,
            )
            raise _error(502, "REPLICATE_ERROR", str(err_msg))

        output_url = _extract_url(prediction.output)
        if not output_url:
            logger.error(
                "remove_text_empty_output host=%s type=%s",
                image_host,
                type(prediction.output).__name__,
            )
            raise _error(502, "REPLICATE_ERROR", "empty output")

        prediction_id = getattr(prediction, "id", "") or ""
        predict_time = _extract_predict_time(prediction)
        logger.debug(
            "remove_text_done host=%s prediction_id=%s",
            image_host,
            prediction_id[:10],
        )
        _log_replicate_call(
            ctx=ctx, operation="retouch.remove_text_image.replicate", model=resolved_model,
            prediction=prediction, inputs=payload, status="success",
            output=output_url, output_urls=[output_url],
        )
        return ReplicatePredictionResult(
            output=output_url,
            prediction_id=prediction_id,
            predict_time=predict_time,
            ai_request_id=rid,
        )
    except HTTPException as exc:
        if prediction is not None:
            _log_replicate_call(
                ctx=ctx, operation="retouch.remove_text_image.replicate", model=resolved_model,
                prediction=prediction, inputs=payload, status="error", error=exc.detail,
            )
        raise


@traceable(name="image.normalize_human.replicate", run_type="llm")
async def run_face_to_many(
    payload: dict,
    timeout_s: float = 180.0,
    *,
    ai_context: AiCallContext | None = None,
) -> ReplicatePredictionResult:
    """Run fofr/face-to-many stylizer. Returns ReplicatePredictionResult (output=url).

    Async-native: `predictions.async_create` + `async_wait` to capture
    prediction ID for `meta.replicatePredictionId`.

    Output is a list of URL(s); first non-empty is selected. The model itself
    decides count — caller has no `num_outputs` knob exposed (see plan
    Scope quyết định).

    Error taxonomy (raises HTTPException):
      - 422 NO_FACE_DETECTED   — Replicate failed face-detection upstream
      - 422 IMAGE_FETCH_ERROR  — Replicate could not fetch input
      - 429 REPLICATE_RATE_LIMIT
      - 502 REPLICATE_ERROR    — generic upstream failure / empty output
      - 504 TIMEOUT            — exceeded `timeout_s`
    """
    client = get_replicate_client()

    image_host = _host(str(payload.get("image", "")))
    logger.debug(
        "normalize_human_invoke host=%s style=%s denoise=%s instantid=%s",
        image_host,
        payload.get("style"),
        payload.get("denoising_strength"),
        payload.get("instant_id_strength"),
    )

    # NOTE: fofr/face-to-many is a *community* model; `predictions.create(model=...)`
    # only resolves *official* models (e.g. bria/remove-background). Community
    # models must be invoked by `version=` hash — see FACE_TO_MANY_VERSION.
    # The model owner/name is kept on the constant + traceable name for
    # observability; the version is what Replicate actually dispatches on.
    _ = FACE_TO_MANY_MODEL  # referenced for log/trace context (kept importable)
    ctx = ai_context or AiCallContext()
    rid = new_request_id()
    prediction = None
    try:
        try:
            async with replicate_prediction_slot():
                prediction = await create_with_429_retry(
                    lambda: client.predictions.async_create(
                        version=FACE_TO_MANY_VERSION,
                        input=payload,
                    ),
                    label="normalize_human",
                )
                await asyncio.wait_for(prediction.async_wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning(
                "normalize_human_timeout host=%s timeout_s=%s", image_host, timeout_s
            )
            raise _error(504, "TIMEOUT", "Normalize-human timed out") from exc
        except HTTPException:
            raise
        except replicate.exceptions.ReplicateError as exc:
            if getattr(exc, "status", None) == 429:
                logger.warning("normalize_human_rate_limited host=%s", image_host)
                raise _error(429, "REPLICATE_RATE_LIMIT", "Replicate rate limited") from exc
            logger.error("normalize_human_replicate_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc
        except Exception as exc:
            logger.error("normalize_human_unexpected_error host=%s err=%s", image_host, exc)
            raise _error(502, "REPLICATE_ERROR", str(exc)) from exc

        if prediction.status != "succeeded":
            err_msg = prediction.error or "prediction failed"
            # Order matters: no-face check before generic fetch check (a no-face
            # message can also contain the word "detect" which is in fetch
            # keywords).
            if _is_no_face_error(prediction.error):
                logger.warning(
                    "normalize_human_no_face host=%s status=%s err=%s",
                    image_host,
                    prediction.status,
                    err_msg,
                )
                raise _error(422, "NO_FACE_DETECTED", str(err_msg))
            if _is_fetch_error(prediction.error):
                logger.warning(
                    "normalize_human_fetch_error host=%s status=%s err=%s",
                    image_host,
                    prediction.status,
                    err_msg,
                )
                raise _error(422, "IMAGE_FETCH_ERROR", str(err_msg))
            logger.error(
                "normalize_human_non_succeeded host=%s status=%s err=%s",
                image_host,
                prediction.status,
                err_msg,
            )
            raise _error(502, "REPLICATE_ERROR", str(err_msg))

        output = prediction.output
        first = output[0] if isinstance(output, list) and output else output
        output_url = _extract_url(first)
        if not output_url:
            # fofr/face-to-many reports face-detect failure via status="succeeded"
            # + empty output + the "No face detected" exception buried in `logs`,
            # not via `error`. Inspect logs to classify accurately.
            logs = getattr(prediction, "logs", "") or ""
            if _is_no_face_error(logs):
                logger.warning(
                    "normalize_human_no_face_via_logs host=%s",
                    image_host,
                )
                raise _error(
                    422,
                    "NO_FACE_DETECTED",
                    "Reference image: no face detected",
                )
            logger.error(
                "normalize_human_empty_output host=%s type=%s",
                image_host,
                type(output).__name__,
            )
            raise _error(502, "REPLICATE_ERROR", "empty output")

        prediction_id = getattr(prediction, "id", "") or ""
        predict_time = _extract_predict_time(prediction)
        logger.debug(
            "normalize_human_done host=%s prediction_id=%s",
            image_host,
            prediction_id[:10],
        )
        _log_replicate_call(
            ctx=ctx, operation="image.normalize_human.replicate", model=FACE_TO_MANY_MODEL,
            prediction=prediction, inputs=payload, status="success",
            output=output_url, output_urls=[output_url],
        )
        return ReplicatePredictionResult(
            output=output_url,
            prediction_id=prediction_id,
            predict_time=predict_time,
            ai_request_id=rid,
        )
    except HTTPException as exc:
        if prediction is not None:
            _log_replicate_call(
                ctx=ctx, operation="image.normalize_human.replicate", model=FACE_TO_MANY_MODEL,
                prediction=prediction, inputs=payload, status="error", error=exc.detail,
            )
        raise
