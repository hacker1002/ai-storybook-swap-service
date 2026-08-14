"""Unit tests for `StorageServiceRestStorage` (ADR-054) — zero network.

Transport is stubbed with `httpx.MockTransport`: the adapter builds a fresh
`httpx.AsyncClient` per call, so we monkeypatch the module's `httpx.AsyncClient`
symbol to inject a MockTransport handler. Every case asserts wire behaviour
(headers, `upsert` query, retry count) or error mapping — no real :8200.
"""

from __future__ import annotations

import httpx
import pytest

import src.storage.storage_service_rest as mod
from src.storage.errors import StorageUploadError
from src.storage.storage_service_rest import StorageServiceRestStorage

_BASE = "http://127.0.0.1:8200"
_PUBLIC = "http://localhost:8200"
_BUCKET = "storybook-assets"
_KEY = "editor-assets/1723600000-abcd.png"
_API_KEY = "super-secret-swap-key"


def _adapter() -> StorageServiceRestStorage:
    return StorageServiceRestStorage(
        base_url=_BASE, api_key=_API_KEY, public_base_url=_PUBLIC, default_bucket=_BUCKET
    )


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Route every `httpx.AsyncClient(...)` built inside the module through a
    MockTransport running `handler`. Returns a list that captures each request."""
    captured: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    return captured


def _service_url(bucket: str, key: str) -> str:
    return f"{_PUBLIC}/files/{bucket}/{key}"


# -- upload happy path -------------------------------------------------------

async def test_upload_201_returns_data_url_and_sends_headers(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"success": True, "data": {
                "bucket": _BUCKET, "key": _KEY, "url": _service_url(_BUCKET, _KEY),
                "etag": "abc", "bytes": 4, "deduped": False,
            }},
        )

    captured = _patch_transport(monkeypatch, handler)
    url = await _adapter().upload(_KEY, b"data", "image/png")

    assert url == _service_url(_BUCKET, _KEY)
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "PUT"
    assert req.headers["X-API-Key"] == _API_KEY
    assert req.headers["Content-Type"] == "image/png"
    # Protocol default upsert=True → query upsert=true.
    assert req.url.params.get("upsert") == "true"


async def test_upload_explicit_upsert_false_sends_false(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"success": True, "data": {"url": _service_url(_BUCKET, _KEY)}}
        )

    captured = _patch_transport(monkeypatch, handler)
    await _adapter().upload(_KEY, b"x", "image/png", upsert=False)
    assert captured[0].url.params.get("upsert") == "false"


# -- error mapping (no retry) ------------------------------------------------

async def test_upload_409_maps_status_code_and_does_not_retry(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"success": False, "error": {"code": "ALREADY_EXISTS", "message": "exists"}}
        )

    captured = _patch_transport(monkeypatch, handler)
    with pytest.raises(StorageUploadError) as ei:
        await _adapter().upload(_KEY, b"x", "image/png", upsert=False)

    assert ei.value.status_code == 409
    assert "ALREADY_EXISTS" in ei.value.reason
    assert len(captured) == 1  # HTTP error → NOT retried


async def test_upload_2xx_missing_data_url_raises_malformed(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"bucket": _BUCKET}})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(StorageUploadError) as ei:
        await _adapter().upload(_KEY, b"x", "image/png")
    assert "MALFORMED_RESPONSE" in ei.value.reason


# -- transport retry ---------------------------------------------------------

async def test_upload_transport_error_retries_thrice_then_raises(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=req)

    captured = _patch_transport(monkeypatch, handler)
    # Zero backoff so the test is fast + deterministic.
    monkeypatch.setattr(mod, "_UPLOAD_RETRY_BACKOFF_S", (0.0, 0.0))

    with pytest.raises(StorageUploadError):
        await _adapter().upload(_KEY, b"x", "image/png", upsert=False)

    assert len(captured) == 3
    # Retries (#2, #3) force upsert=true even though caller passed upsert=False.
    assert captured[0].url.params.get("upsert") == "false"
    assert captured[1].url.params.get("upsert") == "true"
    assert captured[2].url.params.get("upsert") == "true"


# -- public_url invariant ----------------------------------------------------

async def test_public_url_equals_service_returned_url(monkeypatch):
    """The client-side `public_url()` builder MUST match the service's `data.url`
    for the same (bucket, key) — the anti-drift invariant."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"success": True, "data": {"url": _service_url(_BUCKET, _KEY)}}
        )

    _patch_transport(monkeypatch, handler)
    returned = await _adapter().upload(_KEY, b"x", "image/png")
    assert _adapter().public_url(_KEY) == returned


def test_public_url_shape_is_files_path():
    assert _adapter().public_url(_KEY) == f"{_PUBLIC}/files/{_BUCKET}/{_KEY}"
    # Leading slash on the key is normalized, never doubled.
    assert _adapter().public_url("/" + _KEY) == f"{_PUBLIC}/files/{_BUCKET}/{_KEY}"


# -- sign + delete -----------------------------------------------------------

async def test_sign_returns_signed_url(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/api/storage/sign")
        return httpx.Response(
            200, json={"success": True, "data": {"signed_url": "http://x/files/s?token=t"}}
        )

    _patch_transport(monkeypatch, handler)
    assert await _adapter().create_signed_url(_KEY, 3600) == "http://x/files/s?token=t"


async def test_delete_swallows_http_error(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "BOOM"}})

    _patch_transport(monkeypatch, handler)
    # Best-effort — never raises.
    assert await _adapter().delete(_KEY) is None


async def test_delete_swallows_transport_error(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset", request=req)

    _patch_transport(monkeypatch, handler)
    assert await _adapter().delete(_KEY) is None


# -- secret hygiene ----------------------------------------------------------

async def test_api_key_never_in_exception_string(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "error": {"code": "X", "message": "y"}})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(StorageUploadError) as ei:
        await _adapter().upload(_KEY, b"x", "image/png")
    assert _API_KEY not in str(ei.value)
