"""Request/response sanitizers + reference-file lift for the AI log.

Ported from `ai-storybook-python-api/src/services/ai_usage/sanitize.py`. The
sanitizer core (secret/base64 scrub, `extract_ref_blobs`/`extract_output_blobs`
lift, `_sniff_mime` magic sniff) is VERBATIM. ONE forced divergence: this service
has no synchronous content-addressed Storage lib (`persist_sync`), so
`build_ref_metadata` records a file by its content HASH + byte length + mime WITHOUT
re-hosting it (no `url` for raw bytes). That still satisfies the security boundary
below — nothing base64/binary reaches the row, only URL or length metadata.
`_sniff_mime` is inlined here (image-api kept it in `storage/content_store.py`).

Security boundary: NOTHING that reaches `ai_service_logs.request`/`response` may
contain a base64 blob, an API key, or an Authorization header. This module is the
one place that strips them.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Content-addressed prefixes: file INPUTS → `ai-logs/`, raw OUTPUTS → `ai-logs/outputs/`.
_INPUT_PREFIX = "ai-logs"
OUTPUT_PREFIX = "ai-logs/outputs"

# Response text cap 8KB. Request text is unbounded (bytes are externalized).
MAX_RESPONSE_STR = 8 * 1024

# Key substrings whose value must be redacted (case-insensitive).
_SECRET_KEY_RE = re.compile(
    r"(api[-_]?key|authorization|x[-_]api[-_]key|secret|token|password|bearer)",
    re.IGNORECASE,
)
# Keys whose value is expected to be a base64 image blob → replace with metadata.
_BASE64_KEY_RE = re.compile(
    r"(base64|b64|image_?bytes|imagedata)", re.IGNORECASE
)
# A whitespace-free base64/base64url blob of meaningful length (prose has spaces/
# newlines → never matches, so real prompts are preserved).
_BASE64_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{256,}$")
_DATA_URI_RE = re.compile(r"^data:[^;,]*;base64,", re.IGNORECASE)
# A JSON key under which an http(s) URL is treated as a reference file worth
# logging (Gemini `image_url`, Replicate `image`/`img`/`mask`, remix
# `reference_image_url`/`target_base_image_url`). Base64/bytes refs are lifted by
# decode + magic-sniff regardless of key, so this gate only scopes URL collection.
_REF_URL_KEY_RE = re.compile(r"(image|img|photo|picture|mask|url|sheet|reference)", re.IGNORECASE)
# Bound the per-call ref upload work (each bytes-ref = one blocking Storage PUT
# in the insert worker thread); a scene tops out ~7 refs, so 12 is generous.
_MAX_REF_BLOBS = 12

# OUTPUT-only key skip: Gemini 3 image responses ride an opaque, encrypted
# `thought_signature` (langchain surfaces it as `content[].extras.signature`) — a
# ~3.5MB NON-image base64 blob meant for multi-turn reasoning continuation, which
# our one-shot calls never replay. Lifting it wrote a useless 3.5MB `.bin` to
# Storage on EVERY Gemini image call. Skipping it (output side only, see
# `extract_output_blobs`) keeps just the real image; INPUT ref extraction is
# unchanged (accept-all).
_OUTPUT_SKIP_KEY_RE = re.compile(r"(signature|thought|extras)", re.IGNORECASE)

_DEFAULT_MIME = "application/octet-stream"


def _sniff_mime(head: bytes) -> str | None:
    """Magic-byte mime sniff (image ⊕ audio ⊕ pdf ⊕ video). None if unrecognized.

    Inlined verbatim from image-api `storage/content_store.py::_sniff_mime` (that
    module does not exist here). Pure stdlib — no Pillow/numpy dependency.
    """
    if not head:
        return None
    # JPEG: FF D8 FF
    if len(head) >= 3 and head[0] == 0xFF and head[1] == 0xD8 and head[2] == 0xFF:
        return "image/jpeg"
    # PNG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # GIF
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    # RIFF container: WEBP (image) / WAVE (audio)
    if len(head) >= 12 and head[0:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image/webp"
        if head[8:12] == b"WAVE":
            return "audio/wav"
    # PDF
    if head.startswith(b"%PDF"):
        return "application/pdf"
    # ISO-BMFF (mp4/m4a): `....ftyp<brand>`
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand[:3] == b"M4A" or brand == b"M4A ":
            return "audio/mp4"
        return "video/mp4"
    # MP3: ID3 tag or MPEG-1 Layer-3 frame sync (FF Ex/Fx)
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    # SVG text heuristic
    stripped = head.lstrip(b"\xef\xbb\xbf")
    lowered = stripped.lstrip().lower()
    if lowered.startswith(b"<svg"):
        return "image/svg+xml"
    if lowered.startswith(b"<?xml") and b"<svg" in lowered[:256]:
        return "image/svg+xml"
    return None


def _omit_base64(value: str) -> dict:
    return {"_omitted": "base64", "bytes": len(value)}


def _sanitize_value(value: Any, max_str: int | None, depth: int) -> Any:
    if depth > 12:
        return "[max-depth]"
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            key = str(k)
            if _SECRET_KEY_RE.search(key):
                out[key] = "[REDACTED]"
            elif _BASE64_KEY_RE.search(key) and isinstance(v, str):
                out[key] = _omit_base64(v)
            else:
                out[key] = _sanitize_value(v, max_str, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v, max_str, depth + 1) for v in value]
    if isinstance(value, str):
        if _DATA_URI_RE.match(value) or _BASE64_BLOB_RE.match(value):
            return _omit_base64(value)
        if max_str is not None and len(value) > max_str:
            return value[:max_str] + f"...[truncated {len(value) - max_str} chars]"
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"_omitted": "bytes", "bytes": len(value)}
    return value  # int / float / bool / None — JSONB-safe


def _sanitize(payload: Any, max_str: int | None) -> Any:
    try:
        return _sanitize_value(payload, max_str, 0)
    except Exception as e:  # noqa: BLE001 — sanitizer must never break the log path
        logger.warning("ai_usage_sanitize_error reason=%s", e)
        return {"sanitize_error": str(e)}


def sanitize_request(payload: Any) -> dict:
    """JSONB-safe request: base64→metadata, secrets→[REDACTED]. Text UNBOUNDED
    (bytes are externalized to Storage, so a long prompt is safe to keep whole)."""
    result = _sanitize(payload, None)
    return result if isinstance(result, dict) else {"value": result}


def sanitize_response(payload: Any) -> Any:
    """JSONB-safe response: same rules, text capped at 8KB (audio/image → URL only)."""
    return _sanitize(payload, MAX_RESPONSE_STR)


def build_ref_metadata(
    data: bytes | bytearray | str,
    *,
    mime: str | None = None,
    url: str | None = None,
    prefix: str = _INPUT_PREFIX,
) -> dict:
    """Metadata for a reference/output file — recorded by content, never re-hosted.

    DIVERGENCE from image-api: this service has no synchronous content-addressed
    Storage lib (`persist_sync`), so raw bytes are recorded as best-effort meta
    `{sha256, bytes[, mime]}` WITHOUT a `url` — the file is still identified by its
    content hash + length, and NO base64/binary reaches the row (the security
    invariant). A source URL (str) → `{url[, mime]}`, logged as-is. Never raises
    (caller is the fire-and-forget insert path). `prefix` is kept in the signature
    for call-site parity with image-api (input vs output) but is unused here.
    """
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        sha = hashlib.sha256(raw).hexdigest()
        meta: dict = {"sha256": sha, "bytes": len(raw)}
        resolved_mime = mime or _sniff_mime(raw[:256])
        if resolved_mime:
            meta["mime"] = resolved_mime
        return meta

    # Source-URL ref — log url, do not fetch/re-upload.
    meta = {"url": url if url is not None else str(data)}
    if mime:
        meta["mime"] = mime
    return meta


def _b64_to_file_bytes(value: str) -> tuple[bytes, str | None] | None:
    """Decode a data-URI or raw base64 string to `(raw_bytes, mime|None)` IFF it
    decodes to NON-EMPTY bytes (accept-all — audio/video/pdf inputs are persisted
    too). Mime = magic-byte sniff ⊕ declared data-URI header; unknown → None.
    `None` only for un-decodable input."""
    mime_hint: str | None = None
    payload = value
    if _DATA_URI_RE.match(value):
        head, _, payload = value.partition(",")
        m = re.match(r"data:([^;,]+)", head)
        if m:
            mime_hint = m.group(1) or None
    elif not _BASE64_BLOB_RE.match(value):
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001 — malformed base64 is simply "not a ref"
        return None
    if not raw:
        return None
    return raw, (_sniff_mime(raw[:256]) or mime_hint)


def extract_ref_blobs(
    payload: Any,
    *,
    max_refs: int = _MAX_REF_BLOBS,
    skip_key_re: re.Pattern | None = None,
) -> tuple:
    """Lift reference FILES out of a RAW (pre-sanitize) provider request so the
    logger can content-address them into `request.ref_files`.

    Returns a tuple of `(bytes|url, mime|None)` refs — deduped (bytes by sha256,
    urls verbatim), capped at `max_refs`. **NEVER raises**: any failure yields `()`
    because the log path must never break the main request. Run this on the RAW
    request BEFORE `sanitize_request` strips the base64.

      - data-URI / raw base64 that decodes to non-empty bytes → `(bytes, mime|None)`
        (accept-all — image/audio/video/pdf; unknown mime persisted as `bin`).
      - http(s) URL under an image-ish key → `(url, None)` (logged, not re-uploaded).
      - raw `bytes`/`bytearray` → `(bytes, sniffed_mime|None)`.
    """
    out: list[tuple] = []
    seen: set = set()

    def _add_bytes(raw: bytes, mime: str | None) -> None:
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in seen:
            seen.add(digest)
            out.append((raw, mime))

    def _walk(node: Any, key: str, depth: int) -> None:
        if len(out) >= max_refs or depth > 12:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k), depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v, key, depth + 1)
        elif isinstance(node, str):
            # Never externalize a value under a credential-named key (accept-all
            # runs PRE-sanitize, so the `_SECRET_KEY_RE` redaction hasn't fired yet;
            # a ≥256-char base64 secret would otherwise land in the PUBLIC bucket).
            # `skip_key_re` drops additional caller-designated keys (output side: the
            # Gemini thought_signature) so they never reach Storage.
            if _SECRET_KEY_RE.search(key) or (skip_key_re is not None and skip_key_re.search(key)):
                return
            f = _b64_to_file_bytes(node)
            if f is not None:
                _add_bytes(f[0], f[1])
            elif node.startswith(("http://", "https://")) and _REF_URL_KEY_RE.search(key):
                if node not in seen:
                    seen.add(node)
                    out.append((node, None))
        elif isinstance(node, (bytes, bytearray)):
            if skip_key_re is not None and skip_key_re.search(key):
                return
            raw = bytes(node)
            _add_bytes(raw, _sniff_mime(raw[:256]))

    try:
        _walk(payload, "", 0)
    except Exception as e:  # noqa: BLE001 — extraction must never break the log path
        logger.warning("ai_usage_ref_extract_error reason=%s", e)
        return ()
    return tuple(out[:max_refs])


def extract_output_blobs(raw_result: Any, *, max_refs: int = _MAX_REF_BLOBS) -> tuple:
    """Lift RAW AI **output** files out of a pre-sanitize result → `(bytes|url, mime)`
    tuples for content-addressing into `response.output_files`. Mirror of
    `extract_ref_blobs`. **NEVER raises** → `()`.

    Diverges from the input lift in ONE way: keys matching `_OUTPUT_SKIP_KEY_RE`
    (`signature`/`thought`/`extras`) are skipped so the Gemini 3 `thought_signature`
    (`content[].extras.signature`) — a ~3.5MB opaque non-image blob with no
    downstream use in one-shot calls — is never recorded."""
    return extract_ref_blobs(raw_result, max_refs=max_refs, skip_key_re=_OUTPUT_SKIP_KEY_RE)
