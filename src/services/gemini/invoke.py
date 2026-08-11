"""Thin Gemini invoke helper — the ONE place a `ChatGoogleGenerativeAI` client is
built + invoked (ADR-049).

Every Gemini call-site used to hand-roll the SAME four steps: build the ctor
(`ChatGoogleGenerativeAI(model, **vertex_client_kwargs(), safety_settings, **kw)`),
`await llm.ainvoke([msg], config={"run_name": ...})`, pull `usage_metadata`, and
map exceptions. `gemini_ainvoke` folds the first three into one helper and returns
a `GeminiInvokeResult` that already carries FULL token usage (in/out/total) + call
latency.

DESIGN — thin, not a facade (brainstorm §2):
  - **Parts-building + response parsing STAY at the call-site.** The helper never
    touches `HumanMessage(content=parts)` construction nor `extract_image` /
    JSON parsing — those are per-endpoint concerns.
  - **Exceptions propagate RAW.** The helper does NOT classify or wrap. Each
    call-site keeps its own error taxonomy: illustration maps to `LLM_ERROR` with
    an auth-503-non-retry gate; retouch maps to `GEMINI_ERROR`; remix re-maps
    again. Wrapping here would collapse those distinct contracts and hide the
    original exception type from `is_gemini_auth_error` / `classify_gemini_exc`.
    Latency is therefore measured on the SUCCESS path only.
  - **Retry / semaphore are NOT here.** Only image-gen has a concurrency cap +
    429/503 backoff (ADR concurrency `GEMINI_IMAGE_CONCURRENCY`); text/detect do
    not. The illustration/swap core keeps its retry loop + semaphore WRAPPING
    this helper.

P3b PORT NOTE (Phase 05 re-couple): image-api's `gemini_ainvoke` is ALSO the
ADR-050 choke point — it writes one `ai_service_logs` row per call (success/error)
via `src.services.ai_usage`. Phase 02 ported this helper WITHOUT that logging; now
that `src.services.ai_usage` exists (Phase 03), the choke-point logging is
RE-COUPLED here to match image-api. ONE forced divergence: this service has no
synchronous content-addressed Storage lib (`compute_persist_path`), so raw OUTPUT
images are recorded by content-hash metadata inside the logger (via
`build_ref_metadata`) and `GeminiInvokeResult.output_files` stays `()` (no
re-hosted URL). Everything else (rid-before-call, error-row-then-reraise, cost) is
image-api-faithful and fire-and-forget (the log path NEVER raises into the caller).

TEST SEAM: `ChatGoogleGenerativeAI` is a module-level symbol so the whole suite
patches ONE target — `src.services.gemini.invoke.ChatGoogleGenerativeAI` — instead
of per-call-site.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI

from src.services.ai_usage import (
    AiCallContext,
    AiLogEntry,
    compute_cost,
    extract_output_blobs,
    extract_ref_blobs,
    log_ai_request,
    new_request_id,
    sanitize_request,
    sanitize_response,
)
from src.services.gemini.client_kwargs import vertex_client_kwargs
from src.services.gemini.safety import GEMINI_SAFETY_SETTINGS

__all__ = ["GeminiInvokeResult", "gemini_ainvoke"]


@dataclass(frozen=True)
class GeminiInvokeResult:
    """Result of one `gemini_ainvoke` call.

    `message` is the RAW `AIMessage` — the call-site parses `.content` itself
    (`extract_image(...)` for image mode, JSON parse for structured mode). The
    token/latency/model fields expose the audit data.

    `ai_request_id` is the client uuid4 minted BEFORE the provider call (Phase 05
    cores surface it as `data.aiRequestId`). `output_files` stays `()` in this
    service — raw outputs are recorded by content-hash inside the logger, never
    re-hosted (no content-addressed Storage lib here).
    """

    message: Any  # AIMessage — call-site owns `.content` parsing
    model: str  # dispatch id actually sent to the Gemini API
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int
    run_name: str
    ai_request_id: str = ""  # correlation id of this call (surfaced as data.aiRequestId)
    output_files: tuple = ()  # always () here (no content-addressed re-hosting)


def _raw_request(model: str, run_name: str, messages: list, model_kwargs: dict) -> dict:
    """Build the RAW request payload (pre-sanitize) — the shared base for BOTH the
    JSONB-safe `request` (via `sanitize_request`) and reference-image extraction
    (via `extract_ref_blobs`). Base64 image parts are still intact here."""
    return {
        "model": model,
        "run_name": run_name,
        "messages": [getattr(m, "content", m) for m in messages],
        "params": {k: v for k, v in model_kwargs.items() if k != "response_schema"},
    }


def _image_resolution(model_kwargs: dict) -> str | None:
    """Pull `image_size` from `image_config` (image-gen fallback token pricing)."""
    cfg = model_kwargs.get("image_config")
    if isinstance(cfg, dict):
        return cfg.get("image_size")
    return None


def _extract_usage(message: Any) -> tuple[int | None, int | None, int | None]:
    """Pull `(input, output, total)` token counts off an `AIMessage`.

    Upgrade over the old `total_tokens or input_tokens` one-liner every call-site
    ran (which kept only a single number): langchain exposes a `usage_metadata`
    dict with `input_tokens` / `output_tokens` / `total_tokens`. Tolerant of a
    missing / non-dict value (returns all-`None`) so a provider quirk never breaks
    the success path.
    """
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None, None, None
    return (
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
    )


async def gemini_ainvoke(
    *,
    model: str,
    messages: list,
    run_name: str,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    timeout_s: float | None = None,
    safety_settings: Any = GEMINI_SAFETY_SETTINGS,
    ai_context: AiCallContext | None = None,
    **model_kwargs: Any,
) -> GeminiInvokeResult:
    """Build a Vertex `ChatGoogleGenerativeAI`, invoke it, return usage + latency.

    Choke point (ADR-050): EVERY Gemini call is logged fire-and-forget to
    `ai_service_logs` — one row per call (success OR error), so a call-site retry
    loop naturally yields N rows. `ai_context` (optional) carries attribution
    (remix/book/snapshot/job); None → NULL attribution, `operation` still set from
    `run_name`. Latency is measured on BOTH paths. Provider exceptions propagate
    RAW (only the LOG path swallows — inside `log_ai_request`).

    `**model_kwargs` is a pure pass-through to the ctor, so EVERY per-endpoint
    variant is covered without the helper knowing about it: `temperature`,
    `response_modalities` + `image_config` (image mode), `response_mime_type` +
    `response_schema` (JSON mode), `seed`, etc. Callers pass their message list
    already built (`[HumanMessage(content=parts)]`).

    `timeout_s` maps to the ctor `timeout` kwarg only when set (callers that never
    passed a timeout keep the SDK default — do NOT also pass `timeout` in
    `model_kwargs`). Exceptions from `.ainvoke` propagate unchanged.
    """
    llm_kwargs: dict[str, Any] = {
        "model": model,
        **vertex_client_kwargs(),
        "safety_settings": safety_settings,
        **model_kwargs,
    }
    if timeout_s is not None:
        llm_kwargs["timeout"] = timeout_s

    llm = ChatGoogleGenerativeAI(**llm_kwargs)

    config: dict[str, Any] = {"run_name": run_name}
    if tags:
        config["tags"] = tags
    if metadata:
        config["metadata"] = metadata

    ctx = ai_context or AiCallContext()
    rid = new_request_id()  # BEFORE the provider call (correlation id in the envelope)
    raw_request = _raw_request(model, run_name, messages, model_kwargs)
    ref_blobs = extract_ref_blobs(raw_request)  # lift image refs BEFORE sanitize strips them
    request_payload = sanitize_request(raw_request)

    t0 = time.monotonic()
    try:
        message = await llm.ainvoke(messages, config=config)
    except Exception as exc:  # log the error row (latency measured), then re-raise RAW
        latency_ms = int((time.monotonic() - t0) * 1000)
        log_ai_request(
            AiLogEntry(
                provider="gemini", operation=run_name, model=model,
                status="error", context=ctx, request=request_payload,
                error=str(exc)[:2000], latency_ms=latency_ms, ref_blobs=ref_blobs,
            )
        )
        raise

    latency_ms = int((time.monotonic() - t0) * 1000)
    input_tokens, output_tokens, total_tokens = _extract_usage(message)

    usage_for_cost: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    resolution = _image_resolution(model_kwargs)
    if resolution:
        usage_for_cost["resolution"] = resolution

    # Raw output image(s) → recorded by content-hash inside the logger (no re-host).
    output_blobs = extract_output_blobs(getattr(message, "content", None))

    log_ai_request(
        AiLogEntry(
            provider="gemini", operation=run_name, model=model,
            status="success", context=ctx, request=request_payload,
            response=sanitize_response({"content": getattr(message, "content", None)}),
            latency_ms=latency_ms,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens, usage_unit="tokens", usage_amount=total_tokens,
            cost=compute_cost("gemini", model, usage_for_cost), ref_blobs=ref_blobs,
            output_blobs=output_blobs,
        )
    )

    return GeminiInvokeResult(
        message=message,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        run_name=run_name,
        ai_request_id=rid,
    )
