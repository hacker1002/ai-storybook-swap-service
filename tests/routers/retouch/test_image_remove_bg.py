"""POST /api/retouch/image-remove-bg — core mocked (no real Replicate/Storage)."""

from __future__ import annotations

from types import SimpleNamespace

from src.models.requests.image_remove_bg import ImageRemoveBgCoreResult
from src.routers.retouch import image_remove_bg as rmbg


def _payload() -> dict:
    return {"imageUrl": "https://example.test/in.png", "preserveAlpha": True}


def _core_result() -> ImageRemoveBgCoreResult:
    return ImageRemoveBgCoreResult(
        imageUrl="https://cdn.test/nobg.png",
        storagePath="remove-bg-objects/1-in-nobg.png",
        mimeType="image/png",
        replicatePredictionId="pred-1",
        backgroundColor=None,
        aiRequestId="rid-rmbg-1",
        media_url="https://cdn.test/raw.png",
        image_bytes=None,
    )


def test_happy(client, auth_headers, monkeypatch):
    seen: dict = {}

    async def _core(req, *, ai_context=None, operation=None):
        seen["ai_context"] = ai_context
        seen["model"] = req.model
        return _core_result()

    monkeypatch.setattr(rmbg, "image_remove_bg_core", _core)
    resp = client.post("/api/retouch/image-remove-bg", json=_payload(), headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["imageUrl"] == "https://cdn.test/nobg.png"
    assert body["data"]["aiRequestId"] == "rid-rmbg-1"
    assert body["data"]["media_url"] == "https://cdn.test/raw.png"
    assert body["meta"]["replicatePredictionId"] == "pred-1"
    assert seen["ai_context"] is not None


def test_model_selection_passthrough(client, auth_headers, monkeypatch):
    async def _core(req, *, ai_context=None, operation=None):
        assert req.model == "851-labs/background-remover"
        return _core_result()

    monkeypatch.setattr(rmbg, "image_remove_bg_core", _core)
    payload = _payload() | {"model": "851-labs/background-remover"}
    resp = client.post("/api/retouch/image-remove-bg", json=payload, headers=auth_headers)
    assert resp.status_code == 200, resp.text


def test_unsupported_model_422(client, auth_headers, monkeypatch):
    # resolve_model_params (pure) raises before the core is ever reached.
    async def _core(req, *, ai_context=None, operation=None):  # pragma: no cover
        raise AssertionError("core must not run for a bad model")

    monkeypatch.setattr(rmbg, "image_remove_bg_core", _core)
    payload = _payload() | {"model": "totally/unknown-model"}
    resp = client.post("/api/retouch/image-remove-bg", json=payload, headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MODEL"


def test_bad_background_color_400(client, auth_headers):
    payload = _payload() | {"backgroundColor": "notahex"}
    resp = client.post("/api/retouch/image-remove-bg", json=payload, headers=auth_headers)
    assert resp.status_code == 400


def test_requires_bearer(client):
    resp = client.post("/api/retouch/image-remove-bg", json=_payload())
    assert resp.status_code == 401
