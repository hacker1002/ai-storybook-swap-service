"""sanitize.py — secret/base64 scrub + ref lift + best-effort ref metadata."""

from __future__ import annotations

import base64

from src.services.ai_usage.sanitize import (
    build_ref_metadata,
    extract_ref_blobs,
    sanitize_request,
    sanitize_response,
)

# A ≥256-char base64 blob (whitespace-free) → treated as a base64 file, omitted.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
_B64 = base64.b64encode(_PNG).decode()


def test_secrets_redacted():
    out = sanitize_request({"prompt": "draw a cat", "api_key": "sk-123", "Authorization": "Bearer x"})
    assert out["prompt"] == "draw a cat"
    assert out["api_key"] == "[REDACTED]"
    assert out["Authorization"] == "[REDACTED]"


def test_base64_blob_omitted_not_stored():
    out = sanitize_request({"image": _B64})
    assert out["image"] == {"_omitted": "base64", "bytes": len(_B64)}


def test_response_text_capped_at_8kb():
    # Prose (has spaces) → truncated at 8KB; a whitespace-free blob would instead be
    # omitted as base64 (different rule), so use spaced text here.
    long = "word " * 3000  # 15000 chars, spaced
    out = sanitize_response({"text": long})
    assert out["text"].endswith("chars]") and len(out["text"]) < 9000


def test_build_ref_metadata_bytes_records_hash_no_url():
    meta = build_ref_metadata(_PNG)
    assert "url" not in meta  # no re-host in this service
    assert meta["bytes"] == len(_PNG)
    assert meta["mime"] == "image/png"
    assert len(meta["sha256"]) == 64


def test_build_ref_metadata_url_logged_verbatim():
    meta = build_ref_metadata("https://x/y.png", mime="image/png")
    assert meta == {"url": "https://x/y.png", "mime": "image/png"}


def test_extract_ref_blobs_lifts_base64_and_urls():
    payload = {"image": _B64, "reference_image_url": "https://x/ref.png", "secret": _B64}
    blobs = extract_ref_blobs(payload)
    kinds = {("url" if isinstance(d, str) else "bytes") for d, _m in blobs}
    # base64 under a normal key → bytes; url under image key → url; secret key → skipped.
    assert "bytes" in kinds and "url" in kinds
    urls = [d for d, _ in blobs if isinstance(d, str)]
    assert urls == ["https://x/ref.png"]
