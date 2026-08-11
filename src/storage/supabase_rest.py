"""Supabase Storage over the REST API (httpx) — NO Supabase SDK.

`asyncpg` replaced PostgREST for DB, but NOT Storage. image-api uploads through
`sb.storage` (supabase-py); this service is forbidden the SDK, so it talks to the
Storage REST endpoints directly:

| Op         | HTTP |
|------------|------|
| upload     | `POST {url}/storage/v1/object/{bucket}/{path}` — `Authorization: Bearer <key>`, `x-upsert: true`, `Content-Type` |
| public url | `{url}/storage/v1/object/public/{bucket}/{path}` (bucket must be public) |
| signed url | `POST {url}/storage/v1/object/sign/{bucket}/{path}` body `{"expiresIn": n}` |
| delete     | `DELETE {url}/storage/v1/object/{bucket}/{path}` |

Retry parity with image-api `services/storage/uploader.py`: transient transport
errors (stale pooled connection → `RemoteProtocolError`) retry up to 3 attempts
with backoff; a non-transport HTTP status error never retries (won't fix itself).
The service key is a secret → never logged (log bucket/path/size only).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# Transient transport failures retry; a 4xx/5xx API error (bucket-not-found,
# auth) is never retried. Backoff sleeps before attempt 2, then attempt 3.
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_RETRY_BACKOFF_S = (0.5, 1.5)


class StorageUploadError(Exception):
    """Raised when a Supabase Storage write fails. Handler maps to 500."""

    def __init__(self, path: str, bucket: str, reason: str) -> None:
        super().__init__(f"Storage op failed bucket={bucket} path={path}: {reason}")
        self.path = path
        self.bucket = bucket
        self.reason = reason


class SupabaseRestStorage:
    """`AppStorageAdapter` impl over Supabase Storage REST (httpx)."""

    def __init__(
        self,
        base_url: str,
        service_key: str,
        default_bucket: str,
        timeout_s: float = 30.0,
    ) -> None:
        # Trailing slash trimmed so URL joins never double up.
        self._base = base_url.rstrip("/")
        self._key = service_key
        self._default_bucket = default_bucket
        self._timeout = httpx.Timeout(timeout_s)

    # -- helpers ------------------------------------------------------------

    def _bucket(self, bucket: str | None) -> str:
        return bucket or self._default_bucket

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    def _object_url(self, bucket: str, path: str) -> str:
        return f"{self._base}/storage/v1/object/{bucket}/{path.lstrip('/')}"

    def public_url(self, path: str, bucket: str | None = None) -> str:
        bkt = self._bucket(bucket)
        return f"{self._base}/storage/v1/object/public/{bkt}/{path.lstrip('/')}"

    # -- ops ----------------------------------------------------------------

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str,
        bucket: str | None = None,
        upsert: bool = True,
    ) -> str:
        """Upload bytes, retrying transient transport errors → public URL.

        Retries (attempt ≥ 2) force `x-upsert: true`: a first attempt that landed
        server-side before the connection dropped must not 409 the retry.
        """
        bkt = self._bucket(bucket)
        obj_url = self._object_url(bkt, path)
        last_exc: Exception | None = None

        for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
            headers = {
                **self._auth_headers(),
                "Content-Type": content_type,
                "x-upsert": "true" if (upsert or attempt > 1) else "false",
            }
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(obj_url, content=data, headers=headers)
                if resp.status_code >= 400:
                    # Non-transport API error (auth, bucket-not-found) — no retry.
                    logger.warning(
                        "storage_upload_failed bucket=%s path=%s bytes=%d ct=%s status=%d",
                        bkt, path, len(data), content_type, resp.status_code,
                    )
                    raise StorageUploadError(
                        path=path, bucket=bkt,
                        reason=f"HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                logger.debug("storage_upload_ok path=%s bytes=%d ct=%s", path, len(data), content_type)
                return self.public_url(path, bkt)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "storage_upload_retry bucket=%s path=%s attempt=%d/%d error=%s",
                    bkt, path, attempt, _UPLOAD_MAX_ATTEMPTS, exc,
                )
                if attempt < _UPLOAD_MAX_ATTEMPTS:
                    await asyncio.sleep(_UPLOAD_RETRY_BACKOFF_S[attempt - 1])

        logger.warning(
            "storage_upload_failed bucket=%s path=%s bytes=%d ct=%s "
            "error=%s (transport, %d attempts exhausted)",
            bkt, path, len(data), content_type, last_exc, _UPLOAD_MAX_ATTEMPTS,
        )
        raise StorageUploadError(path=path, bucket=bkt, reason=str(last_exc))

    async def create_signed_url(
        self, path: str, expires_in: int, bucket: str | None = None
    ) -> str:
        """POST /object/sign/{bucket}/{path} → absolutized signed URL."""
        bkt = self._bucket(bucket)
        sign_url = f"{self._base}/storage/v1/object/sign/{bkt}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    sign_url,
                    json={"expiresIn": expires_in},
                    headers=self._auth_headers(),
                )
            if resp.status_code >= 400:
                raise StorageUploadError(
                    path=path, bucket=bkt,
                    reason=f"sign HTTP {resp.status_code}: {resp.text[:200]}",
                )
            body = resp.json()
            # Supabase returns {"signedURL": "/object/sign/<bucket>/<path>?token=..."}
            signed = body.get("signedURL") or body.get("signedUrl") if isinstance(body, dict) else None
            if not signed:
                raise StorageUploadError(path=path, bucket=bkt, reason="no signed URL in response")
            return f"{self._base}/storage/v1{signed}" if signed.startswith("/") else signed
        except httpx.TransportError as exc:
            raise StorageUploadError(path=path, bucket=bkt, reason=str(exc)) from exc

    async def delete(self, path: str, bucket: str | None = None) -> None:
        """Best-effort DELETE — never raises (compensation must not mask errors)."""
        bkt = self._bucket(bucket)
        obj_url = self._object_url(bkt, path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request("DELETE", obj_url, headers=self._auth_headers())
            if resp.status_code >= 400:
                logger.warning(
                    "storage_delete_failed bucket=%s path=%s status=%d", bkt, path, resp.status_code,
                )
                return
            logger.info("storage_delete_ok bucket=%s path=%s", bkt, path)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("storage_delete_failed bucket=%s path=%s error=%s", bkt, path, exc)
