"""SSRF guard — block URLs resolving to non-public IPs.

Ported nearly verbatim from image-api `services/ssrf_guard.py`. NO Supabase
coupling here (image-api's `validate_supabase_url` / `validate_storage_host`
helpers are intentionally NOT ported — this service uses a generic env-driven
allowlist instead).

ADDITIVE change vs image-api: `validate_public_url` consults
`settings.ssrf_allowed_hosts_list` (comma-separated env, default empty). A URL
whose host — or `host:port` — is on the allowlist bypasses the private-IP guard.
This is REQUIRED so the service can re-fetch images it just uploaded to its own
Supabase Storage over a loopback URL (`127.0.0.1:54321`) in local dev — that
loopback would otherwise be blocked as a private IP and every 2-step job would
fail. Prod keeps the allowlist empty (public `*.supabase.co` URLs resolve
publicly).
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata",
}


def _allowed_hosts() -> set[str]:
    """Env-driven allowlist (host or host:port), read at call time so a test /
    runtime override of settings takes effect without re-import."""
    from src.config.settings import settings

    return set(settings.ssrf_allowed_hosts_list)


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_allowlisted(hostname: str, port: int | None) -> bool:
    allow = _allowed_hosts()
    if not allow:
        return False
    if hostname in allow:
        return True
    if port is not None and f"{hostname}:{port}" in allow:
        return True
    return False


def validate_public_url(url: str) -> str:
    """Validate URL is public. Raise 400 SSRF_BLOCKED on failure.

    An allowlisted host/`host:port` (env `SSRF_ALLOWED_HOSTS`) bypasses the
    private-IP resolution check — but the hard `_BLOCKED_HOSTNAMES` set (GCP
    metadata etc.) is NEVER bypassable.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        logger.warning("ssrf_blocked scheme=%s", parsed.scheme)
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "Invalid URL scheme"}},
        )

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "Missing hostname"}},
        )

    host_lower = hostname.lower()
    if host_lower in _BLOCKED_HOSTNAMES:
        logger.warning("ssrf_blocked host=%s", host_lower)
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "Blocked hostname"}},
        )

    # ADDITIVE: allowlisted internal Storage host (e.g. 127.0.0.1:54321) bypasses
    # the private-IP guard so the service can re-fetch its own uploads locally.
    if _host_allowlisted(host_lower, parsed.port):
        logger.debug("ssrf_allowlisted host=%s port=%s", host_lower, parsed.port)
        return url

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        logger.warning("ssrf_dns_fail host=%s err=%s", host_lower, exc)
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "DNS resolution failed"}},
        ) from exc

    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            logger.warning("ssrf_blocked_ip host=%s ip=%s", host_lower, ip_str)
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "URL resolves to non-public address"}},
            )

    return url
