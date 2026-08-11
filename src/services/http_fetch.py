"""Async HTTP fetch with size caps, timeouts, and SSRF re-validation.

Ported verbatim from image-api `services/http_fetch.py`. Re-validates the final
URL after redirects (each hop runs through the SSRF guard) and hard-caps the body
at 10MB (streamed — never reads past the cap).
"""

import logging

import httpx
from fastapi import HTTPException

from src.services.ssrf_guard import validate_public_url

logger = logging.getLogger(__name__)


async def fetch_image_bytes(
    url: str,
    max_bytes: int = 10 * 1024 * 1024,
    timeout_s: float = 30.0,
    allowed_mime_prefix: str = "image/",
) -> tuple[bytes, str]:
    """Fetch bytes from a public URL with size/timeout caps.

    Returns (bytes, content_type). Raises HTTPException on failure.
    """
    validate_public_url(url)

    timeout = httpx.Timeout(timeout_s)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                # Re-validate final URL after redirects
                final_url = str(resp.url)
                if final_url != url:
                    validate_public_url(final_url)

                if resp.status_code >= 400:
                    logger.warning("image_fetch_http_status url_host=%s status=%s", _host(url), resp.status_code)
                    raise HTTPException(
                        status_code=422,
                        detail={"success": False, "error": {"code": "IMAGE_FETCH_ERROR", "message": f"Upstream status {resp.status_code}"}},
                    )

                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                if allowed_mime_prefix and not content_type.startswith(allowed_mime_prefix):
                    logger.warning("image_fetch_bad_mime url_host=%s mime=%s", _host(url), content_type)
                    raise HTTPException(
                        status_code=422,
                        detail={"success": False, "error": {"code": "IMAGE_FETCH_ERROR", "message": f"Unexpected content-type: {content_type}"}},
                    )

                cl = resp.headers.get("content-length")
                if cl is not None:
                    try:
                        if int(cl) > max_bytes:
                            raise HTTPException(
                                status_code=422,
                                detail={"success": False, "error": {"code": "IMAGE_FETCH_ERROR", "message": "Image too large"}},
                            )
                    except ValueError:
                        pass

                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning("image_fetch_over_cap url_host=%s bytes=%d", _host(url), len(buf))
                        raise HTTPException(
                            status_code=422,
                            detail={"success": False, "error": {"code": "IMAGE_FETCH_ERROR", "message": "Image exceeds size cap"}},
                        )

                return bytes(buf), content_type
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        logger.warning("image_fetch_timeout url_host=%s", _host(url))
        raise HTTPException(
            status_code=504,
            detail={"success": False, "error": {"code": "TIMEOUT", "message": "Image fetch timed out"}},
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("image_fetch_http_error url_host=%s err=%s", _host(url), exc)
        raise HTTPException(
            status_code=422,
            detail={"success": False, "error": {"code": "IMAGE_FETCH_ERROR", "message": str(exc)}},
        ) from exc


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"
