"""POST /api/retouch/edit-object-image — seams mocked (no real Gemini/Storage).

Asserts transport wiring (request → Gemini invoke → response envelope + meta),
the UNSUPPORTED_MODEL / template-missing / Gemini-error → image-api-envelope paths,
and the editor-session auth gate.
"""

from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from src.routers.retouch import edit_object_image as eoi


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _payload() -> dict:
    return {
        "prompt": "add a red bow",
        "imageUrl": "https://example.test/source.png",
        "aspectRatio": "1:1",
        "imageSize": "1K",
    }


def _gemini_result(png: bytes) -> SimpleNamespace:
    data_uri = f"data:image/png;base64,{_b64(png)}"
    message = SimpleNamespace(
        content=[{"type": "image_url", "image_url": data_uri}],
        usage_metadata={"total_tokens": 321},
    )
    return SimpleNamespace(message=message, ai_request_id="rid-edit-1", model="gemini-x")


def _wire_happy(monkeypatch, png: bytes) -> dict:
    calls: dict = {}

    async def _tmpl(name):
        return ("PROMPT {%request.prompt%} {%request.reference_guide%}", "gemini-3-pro-image")

    async def _fetch(url, *, max_bytes, timeout_s):
        return png, "image/png"

    async def _invoke(**kwargs):
        calls["invoke"] = kwargs
        return _gemini_result(png)

    async def _upload(path, body, content_type="image/png", **kw):
        calls["upload_path"] = path
        return "https://cdn.test/edit-out.png"

    monkeypatch.setattr(eoi, "fetch_template_row", _tmpl)
    monkeypatch.setattr(eoi, "fetch_image_bytes", _fetch)
    monkeypatch.setattr(eoi, "gemini_ainvoke", _invoke)
    monkeypatch.setattr(eoi, "upload_bytes", _upload)
    return calls


def test_happy(client, auth_headers, monkeypatch):
    png = _png_bytes()
    calls = _wire_happy(monkeypatch, png)
    resp = client.post("/api/retouch/edit-object-image", json=_payload(), headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["imageUrl"] == "https://cdn.test/edit-out.png"
    assert body["data"]["aiRequestId"] == "rid-edit-1"
    assert body["meta"]["model"] == "gemini-3-pro-image"
    assert body["meta"]["tokenUsage"] == 321
    # image_config forwarded to Gemini from the body
    assert calls["invoke"]["image_config"] == {"aspect_ratio": "1:1", "image_size": "1K"}
    assert calls["upload_path"].startswith("edit-objects/")


def test_requires_bearer(client, monkeypatch):
    _wire_happy(monkeypatch, _png_bytes())
    resp = client.post("/api/retouch/edit-object-image", json=_payload())
    assert resp.status_code == 401


def test_template_missing_500(client, auth_headers, monkeypatch):
    from src.services.prompt_loader import PromptTemplateNotFound

    async def _tmpl(name):
        raise PromptTemplateNotFound("missing")

    monkeypatch.setattr(eoi, "fetch_template_row", _tmpl)
    resp = client.post("/api/retouch/edit-object-image", json=_payload(), headers=auth_headers)
    assert resp.status_code == 500, resp.text
    # image-api envelope via raw HTTPException → {detail:{success,error}}
    assert resp.json()["detail"]["error"]["code"] == "PROMPT_TEMPLATE_NOT_FOUND"


def test_unsupported_model_422(client, auth_headers, monkeypatch):
    async def _tmpl(name):
        return ("PROMPT", "gemini-3-pro-image")

    monkeypatch.setattr(eoi, "fetch_template_row", _tmpl)
    payload = _payload() | {"modelParams": {"model": "not-a-real-model"}}
    resp = client.post("/api/retouch/edit-object-image", json=payload, headers=auth_headers)
    assert resp.status_code == 422, resp.text
    # RemixDomainError dedicated handler → flat {success,error}
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "UNSUPPORTED_MODEL"


def test_gemini_error_mapped(client, auth_headers, monkeypatch):
    png = _png_bytes()

    async def _tmpl(name):
        return ("PROMPT", "gemini-3-pro-image")

    async def _fetch(url, *, max_bytes, timeout_s):
        return png, "image/png"

    async def _boom(**kwargs):
        raise RuntimeError("safety blocked")

    monkeypatch.setattr(eoi, "fetch_template_row", _tmpl)
    monkeypatch.setattr(eoi, "fetch_image_bytes", _fetch)
    monkeypatch.setattr(eoi, "gemini_ainvoke", _boom)
    resp = client.post("/api/retouch/edit-object-image", json=_payload(), headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"]["code"] == "SAFETY_FILTER_BLOCKED"


def test_body_extra_forbidden_400(client, auth_headers):
    resp = client.post(
        "/api/retouch/edit-object-image",
        json=_payload() | {"bogusField": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 400
