"""`AppStorageAdapter` impl over the self-hosted storage service (ADR-054).

The storage-service backend for the single `AppStorageAdapter` seam. Speaks the
service's write/sign/delete contract (design `storage-service/03-http-api.md`) over
a loopback `X-API-Key` (S2S) connection. `SupabaseRestStorage` stays the rollback
path — the wiring factory picks between them by env presence.

Ported from image-api `services/storage/storage_service_client.py` (contract +
envelope parse + error mapping), but transport is rewritten ASYNC (per-call
`httpx.AsyncClient`, retry loop) to match this service's async Protocol — image-api
is sync because its callers wrap the blocking leg in `asyncio.to_thread`.

HTTP contract:
    PUT    {base}/api/storage/objects/{bucket}/{key}?upsert={true|false}
           headers: X-API-Key, Content-Type: <mime>   body: raw bytes
           201 (new) | 200 (upsert) | 409 ALREADY_EXISTS | 413 | 415 | 507 | 5xx
           → {"success":true,"data":{bucket,key,url,etag,bytes,deduped}}
    POST   {base}/api/storage/sign        {bucket,key,expires_in} → data.signed_url
    DELETE {base}/api/storage/objects/{bucket}/{key}  → data.deleted  (always 200)
    READ   {public_base}/files/{bucket}/{key}          (nginx, no auth)

`data.url` from the PUT response is the ONE source of truth for the persisted URL —
never re-joined client-side. Retry policy mirrors `SupabaseRestStorage`: a transient
transport error (stale pooled connection reset) retries up to 3 attempts with
backoff, forcing `upsert=true` on retries so a write that landed before the reset
does not 409; a non-transport HTTP error never retries (won't fix itself). The API
key + body bytes are NEVER logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from src.storage.errors import StorageUploadError

logger = logging.getLogger(__name__)

# Transient transport failures retry; a 4xx/5xx API error never retries. Backoff
# sleeps before attempt 2, then attempt 3. Timeout mirrors image-api's storage
# client: connect 10s, read/write 120s (large sheets / audio blobs).
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_RETRY_BACKOFF_S = (0.5, 1.5)
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)


class StorageServiceRestStorage:
    """`AppStorageAdapter` impl over the storage-service HTTP contract (httpx async)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        public_base_url: str,
        default_bucket: str,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        # Loopback base for write/sign/delete (S2S). Trailing slash trimmed so URL
        # joins never double up (settings already strips, belt-and-suspenders).
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        # Public base builds the persisted READ URL (nginx `/files/...`). MUST match
        # what the service returns in `data.url` — asserted by unit test.
        self._public_base = public_base_url.rstrip("/")
        self._default_bucket = default_bucket
        self._timeout = timeout or _HTTP_TIMEOUT

    # -- helpers ------------------------------------------------------------

    def _bucket(self, bucket: str | None) -> str:
        return bucket or self._default_bucket

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"X-API-Key": self._api_key}
        if extra:
            h.update(extra)
        return h

    def _object_url(self, bucket: str, path: str) -> str:
        """`{base}/api/storage/objects/{bucket}/{key}` — quote each path SEGMENT
        (`/` preserved as separator) so a key with spaces/unicode is transport-safe.
        Defence-in-depth on top of the path builders; the service also validates
        the key grammar."""
        key = path.lstrip("/")
        q_bucket = quote(bucket, safe="")
        q_key = "/".join(quote(seg, safe="") for seg in key.split("/"))
        return f"{self._base}/api/storage/objects/{q_bucket}/{q_key}"

    def _envelope_data(self, resp: httpx.Response, path: str, bucket: str) -> dict[str, Any]:
        """Parse `{success, data}` on 2xx; raise `StorageUploadError` (with the
        upstream `status_code` + envelope `error.code`/`message`) otherwise."""
        if resp.status_code >= 400:
            code, message = "HTTP_ERROR", resp.reason_phrase or ""
            try:
                body = resp.json()
                err = (body or {}).get("error") or {}
                code = err.get("code") or code
                message = err.get("message") or message
            except Exception:  # noqa: BLE001 — non-JSON error body → keep defaults
                pass
            raise StorageUploadError(
                path=path, bucket=bucket,
                reason=f"{code}: {message}", status_code=resp.status_code,
            )
        body = resp.json()
        data = (body or {}).get("data")
        if not isinstance(data, dict):
            raise StorageUploadError(
                path=path, bucket=bucket,
                reason="MALFORMED_RESPONSE: missing data envelope",
                status_code=resp.status_code,
            )
        return data

    # -- ops ----------------------------------------------------------------

    def public_url(self, path: str, bucket: str | None = None) -> str:
        """Deterministic READ URL `{public_base}/files/{bucket}/{key}` (no I/O).

        MUST equal the `data.url` the service returns from PUT (unit-test invariant)
        — that equality is what lets `upload()` trust `data.url` while callers that
        already know (bucket, key) can rebuild the same URL without a round-trip."""
        bkt = self._bucket(bucket)
        return f"{self._public_base}/files/{bkt}/{path.lstrip('/')}"

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str,
        bucket: str | None = None,
        upsert: bool = True,
    ) -> str:
        """PUT bytes → the service-built public URL (`data.url`, the ONE source of
        truth). Retries (attempt ≥ 2) force `upsert=true`: a first attempt that
        landed server-side before the connection dropped must not 409 the retry.
        A non-transport HTTP error (413/415/507/auth) bubbles immediately."""
        bkt = self._bucket(bucket)
        obj_url = self._object_url(bkt, path)
        last_exc: Exception | None = None

        for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
            params = {"upsert": "true" if (upsert or attempt > 1) else "false"}
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.put(
                        obj_url,
                        params=params,
                        headers=self._headers({"Content-Type": content_type}),
                        content=data,
                    )
                envelope = self._envelope_data(resp, path, bkt)
                url = envelope.get("url")
                if not isinstance(url, str) or not url:
                    raise StorageUploadError(
                        path=path, bucket=bkt,
                        reason="MALFORMED_RESPONSE: missing data.url",
                        status_code=resp.status_code,
                    )
                logger.debug(
                    "storage_upload_ok bucket=%s path=%s bytes=%d ct=%s status=%d deduped=%s",
                    bkt, path, len(data), content_type, resp.status_code, envelope.get("deduped"),
                )
                return url
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "storage_upload_retry bucket=%s path=%s attempt=%d/%d error=%s",
                    bkt, path, attempt, _UPLOAD_MAX_ATTEMPTS, exc,
                )
                if attempt < _UPLOAD_MAX_ATTEMPTS:
                    await asyncio.sleep(_UPLOAD_RETRY_BACKOFF_S[attempt - 1])
            # StorageUploadError (HTTP status) → NOT caught here, bubbles immediately.

        logger.warning(
            "storage_upload_failed bucket=%s path=%s bytes=%d ct=%s "
            "error=%s (transport, %d attempts exhausted)",
            bkt, path, len(data), content_type, last_exc, _UPLOAD_MAX_ATTEMPTS,
        )
        raise StorageUploadError(path=path, bucket=bkt, reason=str(last_exc))

    async def create_signed_url(
        self, path: str, expires_in: int, bucket: str | None = None
    ) -> str:
        """POST /api/storage/sign → `data.signed_url`. No consumer in this service
        yet (Protocol parity + ready for P3c) — implemented, not stubbed."""
        bkt = self._bucket(bucket)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/api/storage/sign",
                    headers=self._headers({"Content-Type": "application/json"}),
                    json={"bucket": bkt, "key": path.lstrip("/"), "expires_in": expires_in},
                )
            data = self._envelope_data(resp, path, bkt)
            url = data.get("signed_url")
            if not isinstance(url, str) or not url:
                raise StorageUploadError(
                    path=path, bucket=bkt,
                    reason="MALFORMED_RESPONSE: missing data.signed_url",
                    status_code=resp.status_code,
                )
            return url
        except httpx.TransportError as exc:
            raise StorageUploadError(path=path, bucket=bkt, reason=str(exc)) from exc

    async def delete(self, path: str, bucket: str | None = None) -> None:
        """Best-effort DELETE — never raises (compensation must not mask errors).

        Parity with `SupabaseRestStorage.delete`: swallow transport + HTTP errors,
        log a warning only."""
        bkt = self._bucket(bucket)
        obj_url = self._object_url(bkt, path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.delete(obj_url, headers=self._headers())
            if resp.status_code >= 400:
                logger.warning(
                    "storage_delete_failed bucket=%s path=%s status=%d",
                    bkt, path, resp.status_code,
                )
                return
            logger.info("storage_delete_ok bucket=%s path=%s", bkt, path)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("storage_delete_failed bucket=%s path=%s error=%s", bkt, path, exc)
