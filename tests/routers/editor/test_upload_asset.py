"""POST /api/editor/assets — proxy upload (P3c Gap 1). Storage seam mocked.

Security-focused: MIME is content-SNIFFED (spoofed type rejected), size capped,
path server-generated. `upload_bytes` is mocked so no real Storage I/O runs.
"""

from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from src.routers.editor import upload_asset as ua


def _png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _mock_upload(monkeypatch) -> dict:
    seen: dict = {}

    async def _upload(path, body, content_type, **kw):
        seen["path"] = path
        seen["content_type"] = content_type
        return "https://cdn.test/editor-assets/x.png"

    monkeypatch.setattr(ua, "upload_bytes", _upload)
    return seen


def test_happy(client, auth_headers, monkeypatch):
    seen = _mock_upload(monkeypatch)
    resp = client.post(
        "/api/editor/assets", json={"imageBase64": _png_b64()}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["contentType"] == "image/png"
    assert body["data"]["url"] == "https://cdn.test/editor-assets/x.png"
    assert body["data"]["storagePath"].startswith("editor-assets/")
    # server generated the path — client never supplied it
    assert seen["path"].startswith("editor-assets/")


def test_data_uri_prefix_accepted(client, auth_headers, monkeypatch):
    _mock_upload(monkeypatch)
    data_uri = f"data:image/png;base64,{_png_b64()}"
    resp = client.post(
        "/api/editor/assets", json={"imageBase64": data_uri}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text


def test_spoofed_mime_rejected(client, auth_headers, monkeypatch):
    # data-URI CLAIMS png but the bytes are plain text → sniff wins → 400.
    _mock_upload(monkeypatch)
    fake = base64.b64encode(b"this is not an image at all").decode("ascii")
    resp = client.post(
        "/api/editor/assets",
        json={"imageBase64": f"data:image/png;base64,{fake}"},
        headers=auth_headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_malformed_base64_400(client, auth_headers, monkeypatch):
    _mock_upload(monkeypatch)
    resp = client.post(
        "/api/editor/assets", json={"imageBase64": "!!!not base64!!!"}, headers=auth_headers
    )
    assert resp.status_code == 400


def test_over_cap_400(client, auth_headers, monkeypatch):
    _mock_upload(monkeypatch)
    monkeypatch.setattr(ua, "_MAX_ASSET_BYTES", 4)  # tiny cap → any real image exceeds
    resp = client.post(
        "/api/editor/assets", json={"imageBase64": _png_b64()}, headers=auth_headers
    )
    assert resp.status_code == 400, resp.text


def test_extra_field_400(client, auth_headers):
    resp = client.post(
        "/api/editor/assets",
        json={"imageBase64": _png_b64(), "storagePath": "../../etc/passwd.png"},
        headers=auth_headers,
    )
    # client cannot supply a path — extra=forbid rejects it outright
    assert resp.status_code == 400


def test_requires_bearer(client):
    resp = client.post("/api/editor/assets", json={"imageBase64": _png_b64()})
    assert resp.status_code == 401
