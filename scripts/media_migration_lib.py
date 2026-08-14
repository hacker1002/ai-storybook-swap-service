"""Shared helpers for the 3-step Supabase→storage-service media migration
(scan → transfer → rewrite). OPS scripts only — never imported by the service.

Pipeline contract (JSON files under scripts/media-migration-output/):
  1. `media_migration_1_scan_db_urls.py`    → scan-manifest.json
       every old Supabase Storage URL found in the App DB, with the exact
       (table, pk, column) cell each URL string lives in.
  2. `media_migration_2_transfer_blobs.py`  → transfer-results.json
       per unique (bucket, key): downloaded from Supabase Storage REST and
       PUT to the storage service (409 = already there = success).
  3. `media_migration_3_rewrite_db_urls.py` → rewrite-report.json
       for every URL whose blob transferred OK: targeted SQL in-place
       `replace()` on the owning cell — never a read-modify-write from the
       (possibly stale) manifest, so concurrent app writes are never clobbered.

Verify: re-run script 1 — residue must be 0 (or explained) before Supabase
Storage can be decommissioned.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUTPUT_DIR = Path(__file__).resolve().parent / "media-migration-output"
SCAN_MANIFEST = OUTPUT_DIR / "scan-manifest.json"
TRANSFER_RESULTS = OUTPUT_DIR / "transfer-results.json"
REWRITE_REPORT = OUTPUT_DIR / "rewrite-report.json"

# Matches any Supabase Storage object URL as it appears inside a JSONB/text cell.
# Terminators: JSON string quote, backslash (JSON escape), whitespace, angle
# brackets. Captures public/sign/authenticated variants INCLUDING any ?token=
# query so script 3 replaces the whole stale string.
STORAGE_URL_RE = re.compile(
    r"https?://[^\s\"'\\<>]+/storage/v1/object/"
    r"(?:public/|sign/|authenticated/)?[^\s\"'\\<>]+"
)

# Mirror of storage-service `validation/key_grammar.py` — pre-flight so an
# unmigratable key is reported with its rule instead of a blind 400.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_KEY_LEN = 1024
_MAX_SEGMENT_LEN = 255


def key_grammar_violation(key: str) -> str | None:
    """Return the violated storage-service key rule, or None when valid."""
    if not key or len(key) > _MAX_KEY_LEN:
        return "key_length"
    if key.startswith("/") or "\\" in key or "%" in key:
        return "forbidden_char"
    if any(ord(c) < 0x20 for c in key):
        return "control_char"
    segments = key.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            return "empty_or_dot_segment"
        if len(seg) > _MAX_SEGMENT_LEN:
            return "segment_too_long"
        if not _SEGMENT_RE.match(seg):
            return "illegal_segment_char"
    last = segments[-1]
    if "." not in last or last.rsplit(".", 1)[1] == "":
        return "missing_extension"
    return None


def parse_storage_url(url: str) -> dict[str, Any]:
    """Split a Supabase Storage URL into {host, kind, bucket, key, valid, reason}.

    `key` is percent-DECODED (the service PUTs and serves raw keys; its grammar
    forbids `%`). A key that decodes to something grammar-invalid (non-ASCII,
    spaces, no extension) is flagged — those need manual handling.
    """
    parsed = urlparse(url)
    path = parsed.path
    marker = "/storage/v1/object/"
    idx = path.find(marker)
    rest = path[idx + len(marker):]
    kind = "plain"
    for prefix in ("public/", "sign/", "authenticated/"):
        if rest.startswith(prefix):
            kind = prefix.rstrip("/")
            rest = rest[len(prefix):]
            break
    bucket, _, raw_key = rest.partition("/")
    key = unquote(raw_key)
    reason = key_grammar_violation(key) if bucket and key else "unparseable"
    return {
        "host": parsed.netloc,
        "kind": kind,
        "bucket": bucket,
        "key": key,
        "valid": reason is None,
        "reason": reason,
    }


def blob_id(bucket: str, key: str) -> str:
    return f"{bucket}/{key}"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Missing input file: {path} — run the previous step first")
    return json.loads(path.read_text())


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def supabase_download_headers(service_key: str) -> dict[str, str]:
    # sb_secret_ keys are not JWTs: the gateway resolves them from `apikey`,
    # while Bearer-only 400s ("Invalid Compact JWS") — always send BOTH.
    return {"Authorization": f"Bearer {service_key}", "apikey": service_key}


async def supabase_download(
    client: httpx.AsyncClient, base_url: str, service_key: str, bucket: str, key: str
) -> tuple[bytes, str]:
    """Service-role GET of one object → (bytes, content_type). Raises on non-200."""
    url = f"{base_url.rstrip('/')}/storage/v1/object/{bucket}/{key}"
    resp = await client.get(url, headers=supabase_download_headers(service_key))
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "application/octet-stream")


class StoragePutError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"storage service {status} {code}: {message}")
        self.status = status
        self.code = code


async def storage_service_put(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
    upsert: bool,
) -> dict[str, Any]:
    """PUT one object → envelope `data` ({url, etag, bytes, deduped}).
    409 ALREADY_EXISTS raises StoragePutError(status=409) — caller maps to skip."""
    from urllib.parse import quote

    q_key = "/".join(quote(seg, safe="") for seg in key.split("/"))
    url = f"{base_url.rstrip('/')}/api/storage/objects/{quote(bucket, safe='')}/{q_key}"
    resp = await client.put(
        url,
        params={"upsert": "true" if upsert else "false"},
        headers={"X-API-Key": api_key, "Content-Type": content_type},
        content=body,
    )
    if resp.status_code >= 400:
        code, message = "HTTP_ERROR", resp.reason_phrase or ""
        try:
            err = (resp.json() or {}).get("error") or {}
            code = err.get("code") or code
            message = err.get("message") or message
        except Exception:  # noqa: BLE001 — non-JSON error body
            pass
        raise StoragePutError(resp.status_code, code, message)
    data = (resp.json() or {}).get("data") or {}
    if not data.get("url"):
        raise StoragePutError(resp.status_code, "MALFORMED_RESPONSE", "missing data.url")
    return data


def new_public_url(public_base: str, bucket: str, key: str) -> str:
    return f"{public_base.rstrip('/')}/files/{bucket}/{key}"
