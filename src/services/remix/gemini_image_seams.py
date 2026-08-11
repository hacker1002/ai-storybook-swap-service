"""Shared Gemini image-call seams for remix crop-sheet swap cores.

Shared Gemini seams so the multi-target mix core (04) and the sprite-sheet core
reuse the EXACT same:
  - `_gemini_sem` — the ONE module-level concurrency gate (cap=3). Every remix
    swap core acquires THIS instance so the per-process Gemini image-preview rate
    limit is honoured globally. Creating a second semaphore would double the
    effective concurrency → rate-limit storms.
  - `_fetch_one` — SSRF-guarded single-image fetch with PII-free error mapping.
  - `_fetch_and_shrink_hint` — fetch + pre-shrink an identity-hint image
    (never raises; returns `(bytes, None)` or `(None, reason)`).
  - `_finish_reason` / `_SAFETY_FINISH_REASONS` — safety finish-reason inspection.

PII discipline: never log/echo URLs, bytes, or base64; error details carry only
a `which` label (and the caller may add `target_key`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException

from src.services.gemini.payload_budget import (
    BudgetExceededError,
    fit_identity_hint,
)
from src.services.http_fetch import fetch_image_bytes
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)

__all__ = [
    "AUX_FETCH_MAX_BYTES",
    "GEMINI_CONCURRENCY_CAP",
    "_gemini_sem",
    "_SAFETY_FINISH_REASONS",
    "_finish_reason",
    "_fetch_one",
    "_fetch_and_shrink_hint",
]

# Source-art fetch cap for identity-hint aux images (target_base + unchanged).
# Pre-shrink brings them well below this; the cap only guards against pulling a
# decompression-bomb source. Parity with spec §Input caps "10MB/ảnh".
AUX_FETCH_MAX_BYTES = 10 * 1024 * 1024

# Module-level Gemini concurrency gate. Gemini image-preview is globally
# rate-limited per process; this caps concurrent `llm.ainvoke` calls across the
# single-target endpoint (02), the multi-target endpoint (04), and the bulk
# character-swap job. The SINGLE shared instance — do NOT create another.
GEMINI_CONCURRENCY_CAP = 3
_gemini_sem = asyncio.Semaphore(GEMINI_CONCURRENCY_CAP)

# Gemini image-preview returns a candidate (no exception) with these
# finish_reasons on a content/identity block.
_SAFETY_FINISH_REASONS = frozenset({
    "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY",
    "BLOCKLIST", "SPII", "RECITATION",
})


def _finish_reason(response: object) -> Optional[str]:
    """Read finish_reason from langchain response_metadata (camel/snake)."""
    meta = getattr(response, "response_metadata", None)
    if not isinstance(meta, dict):
        return None
    fr = meta.get("finish_reason") or meta.get("finishReason")
    return fr.upper() if isinstance(fr, str) else None


async def _fetch_one(url: str, which: str, max_bytes: int) -> bytes:
    """Fetch one image (SSRF-guarded). Map every failure → RemixDomainError.

    `which` is a PII-free label used only in the error message/details. The raw
    URL is NEVER placed into the error.
    """
    try:
        data, _ct = await fetch_image_bytes(url, max_bytes=max_bytes)
        return data
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        err = detail.get("error", {}) if isinstance(detail, dict) else {}
        inner_code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message", "") if isinstance(err, dict) else ""

        if inner_code == "SSRF_BLOCKED":
            raise RemixDomainError(
                status=400, code="SSRF_BLOCKED",
                message=f"{which} image blocked by SSRF guard",
                details={"which": which},
            ) from exc
        # http_fetch emits two oversize messages: "Image too large"
        # (content-length path) and "Image exceeds size cap" (streamed
        # overflow). Match both → 413 per spec §Error Handling.
        msg_l = str(msg).lower()
        if "too large" in msg_l or "exceeds size cap" in msg_l:
            raise RemixDomainError(
                status=413, code="IMAGE_TOO_LARGE",
                message=f"{which} image exceeds size cap",
                details={"which": which},
            ) from exc
        if exc.status_code == 504:
            raise RemixDomainError(
                status=504, code="TIMEOUT",
                message=f"{which} image fetch timed out",
                details={"which": which},
            ) from exc
        raise RemixDomainError(
            status=502, code="IMAGE_FETCH_ERROR",
            message=f"{which} image fetch failed",
            details={"which": which, "inner_code": inner_code},
        ) from exc
    except RemixDomainError:
        raise
    except Exception as exc:
        raise RemixDomainError(
            status=502, code="IMAGE_FETCH_ERROR",
            message=f"{which} image unexpected fetch failure",
            details={"which": which, "err_type": type(exc).__name__},
        ) from exc


async def _fetch_and_shrink_hint(
    url: str,
    which: str,
    *,
    fit_fn: Callable[[bytes], Awaitable[bytes]] = fit_identity_hint,
) -> tuple[Optional[bytes], Optional[str]]:
    """Fetch (SSRF-guarded) + pre-shrink one identity-hint image.

    Returns `(shrunk_bytes, None)` on success or `(None, reason)` on failure,
    where `reason ∈ {'FETCH_ERROR','DECODE_ERROR'}`. NEVER raises — the caller
    decides whether a given failure is fatal or a skip. `fit_fn` defaults to
    `fit_identity_hint`; a caller may pass a tighter custom fit. `which` is a
    PII-free label only.
    """
    try:
        raw = await _fetch_one(url, which, AUX_FETCH_MAX_BYTES)
    except RemixDomainError:
        return None, "FETCH_ERROR"
    try:
        shrunk = await fit_fn(raw)
    except BudgetExceededError:
        return None, "DECODE_ERROR"
    return shrunk, None
