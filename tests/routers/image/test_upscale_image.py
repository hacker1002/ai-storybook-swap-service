"""POST /api/image/upscale-image — `run_upscale` core mocked (no real Replicate)."""

from __future__ import annotations

from types import SimpleNamespace

from src.routers.image import upscale_image as up


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        imageUrl="https://cdn.test/up.png",
        storagePath="upscale/1-in-x4.png",
        width=1024,
        height=1024,
        mimeType="image/png",
        scale=4.0,
        sourceType="url",
        tileCount=1,
        replicatePredictionIds=["pred-1"],
        fixedRatio=False,
        variant="Anime - anime6B",
        grainApplied=False,
        grain=None,
        ai_request_id="rid-up-1",
    )


def test_happy(client, auth_headers, monkeypatch):
    seen: dict = {}

    async def _run(core_req, *, ai_context=None):
        seen["model"] = core_req.model
        seen["ai_context"] = ai_context
        return _result()

    monkeypatch.setattr(up, "run_upscale", _run)
    resp = client.post(
        "/api/image/upscale-image",
        json={"imageUrl": "https://example.test/in.png", "scale": 4},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["imageUrl"] == "https://cdn.test/up.png"
    assert body["data"]["aiRequestId"] == "rid-up-1"
    assert body["meta"]["variant"] == "Anime - anime6B"
    # omit modelParams → default resolves to xinntao/realesrgan
    assert seen["model"] == "xinntao/realesrgan"


def test_invalid_source_both_422(client, auth_headers):
    resp = client.post(
        "/api/image/upscale-image",
        json={"imageUrl": "https://example.test/in.png", "imageBase64": "AAAA"},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "INVALID_IMAGE_SOURCE"


def test_invalid_source_neither_422(client, auth_headers):
    resp = client.post("/api/image/upscale-image", json={"scale": 2}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_IMAGE_SOURCE"


def test_unsupported_model_422(client, auth_headers, monkeypatch):
    async def _run(core_req, *, ai_context=None):  # pragma: no cover
        raise AssertionError("core must not run")

    monkeypatch.setattr(up, "run_upscale", _run)
    resp = client.post(
        "/api/image/upscale-image",
        json={"imageUrl": "https://example.test/in.png", "modelParams": {"model": "bad/model"}},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MODEL"


def test_extra_field_400(client, auth_headers):
    resp = client.post(
        "/api/image/upscale-image",
        json={"imageUrl": "https://example.test/in.png", "options": {"x": 1}},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_requires_bearer(client):
    resp = client.post("/api/image/upscale-image", json={"imageUrl": "https://example.test/in.png"})
    assert resp.status_code == 401
