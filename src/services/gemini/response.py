"""Domain-neutral helpers for Gemini multimodal response handling.

Extracted from `routers/retouch/edit_object_image.py` so both retouch and
remix endpoints can share image-part extraction and exception classification
without each duplicating logic or each router coupling on FastAPI's
`HTTPException`. Callers map `GeminiResponseError` to their own envelope
(`error_response()` for routers, `RemixDomainError` for service core).

NOTE: error code names here are the *retouch convention* (`NO_IMAGE_RESPONSE`,
`GEMINI_ERROR`). The remix crop-sheet swap specs use slightly different names
(`NO_IMAGE_IN_RESPONSE`, `GEMINI_API_ERROR`) — those callers re-map
locally so this helper stays stable for retouch and other future consumers.
"""

from __future__ import annotations

import base64
from typing import Any

# `google.auth.exceptions` ships transitively with google-genai, but wrap the
# import so a future rename can never crash this module at import time — the
# substring fallback in `is_gemini_auth_error` still classifies auth failures.
try:  # pragma: no cover - trivial import guard
    import google.auth.exceptions as _gauth

    _AUTH_EXC_TYPES: tuple[type[BaseException], ...] = (
        _gauth.DefaultCredentialsError,
        _gauth.RefreshError,
    )
except Exception:  # pragma: no cover
    _AUTH_EXC_TYPES = ()

# Substring fallback for IAM/permission failures surfaced by the Vertex endpoint
# (ClientError 403) and for auth-plumbing exceptions whose type we could not bind.
_AUTH_MSG_MARKERS = ("permission_denied", "unauthenticated", "permission denied")

__all__ = [
    "GeminiResponseError",
    "extract_image",
    "classify_gemini_exc",
    "is_gemini_auth_error",
]


class GeminiResponseError(Exception):
    """Domain-neutral Gemini response failure.

    `status` / `code` / `message` are designed to be mappable 1:1 onto any
    HTTP envelope. NOT coupled to FastAPI.
    """

    __slots__ = ("status", "code", "message")

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message


def extract_image(content: Any) -> tuple[bytes, str]:
    """Extract `(image_bytes, mime)` from an `AIMessage.content`.

    langchain-google-genai returns content as a list mixing text + image
    parts. Image parts appear as either:
      - `{"type": "image_url", "image_url": "data:<mime>;base64,<b64>"}`
      - `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}}`

    Raises `GeminiResponseError(502, "NO_IMAGE_RESPONSE", ...)` if no
    decodable image part is found.
    """
    if isinstance(content, str):
        raise GeminiResponseError(
            502, "NO_IMAGE_RESPONSE", "Gemini returned text-only response"
        )

    parts = content if isinstance(content, list) else [content]
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "image_url":
            continue
        url = part.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        if not isinstance(url, str) or not url.startswith("data:"):
            continue
        header, _, b64 = url.partition(",")
        mime = header.split(";", 1)[0].removeprefix("data:") or "image/png"
        try:
            return base64.b64decode(b64), mime
        except Exception:
            continue

    raise GeminiResponseError(
        502, "NO_IMAGE_RESPONSE", "No inline image in Gemini response"
    )


def is_gemini_auth_error(exc: Exception) -> bool:
    """Is this an ADC/IAM auth failure rather than a model/transport failure?

    After the Vertex cutover (ADR-048) auth failures are NOT "bad API key" — they
    are (1) ADC plumbing: no credential source (`DefaultCredentialsError`) or a
    revoked/expired refresh token (`RefreshError`), raised at `.ainvoke()` call
    time; or (2) IAM/permission from the Vertex endpoint (`PERMISSION_DENIED` /
    `UNAUTHENTICATED`). Type match is authoritative + stable across backends;
    the message substring is the fallback for the IAM-403 case (no bindable type).
    """
    if _AUTH_EXC_TYPES and isinstance(exc, _AUTH_EXC_TYPES):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _AUTH_MSG_MARKERS)


def classify_gemini_exc(exc: Exception) -> tuple[int, str]:
    """Map a langchain/google-genai exception to (http_status, error_code).

    Returns the retouch-convention codes; remix may locally re-map.
    """
    # Auth checked FIRST — a credential/IAM outage is a server config fault, mapped
    # to a clean 503 so it never masquerades as a generic 502 model error and never
    # leaks IAM detail to the client. Public code name is UNCHANGED (contract).
    if is_gemini_auth_error(exc):
        return 503, "GEMINI_ERROR"
    msg = str(exc).lower()
    if "rate limit" in msg or "resource_exhausted" in msg or "429" in msg:
        return 429, "GEMINI_RATE_LIMIT"
    if "safety" in msg or "blocked" in msg:
        return 422, "SAFETY_FILTER_BLOCKED"
    return 502, "GEMINI_ERROR"
