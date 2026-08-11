"""POST /api/remix/build-crop-sheet — REAL stateless compose (Pillow path runs).

Only the network fetch is stubbed (`fetch_image_bytes` → a tiny in-memory PNG); the
crop-sheet composition (frame render, ordinal badge, canvas) executes for real, so
this is the parity anchor for the Pillow + envelope path.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from src.services.remix import crop_sheet_composer


def _tiny_png(color=(200, 30, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (48, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def _payload(n_crops: int = 2) -> dict:
    return {
        "sheet_geometry": {"width": 256, "height": 128},
        "crops": [
            {
                "id": f"c{i}",
                "media_url": f"https://example.test/crop-{i}.png",
                "geometry": {"x": i * 128, "y": 0, "w": 128, "h": 128},
            }
            for i in range(n_crops)
        ],
        "response_format": "base64",
    }


def test_build_crop_sheet_happy(remix_client, auth_headers, monkeypatch):
    async def _fake_fetch(url: str):
        return _tiny_png(), "image/png"

    monkeypatch.setattr(crop_sheet_composer, "fetch_image_bytes", _fake_fetch)

    resp = remix_client.post(
        "/api/remix/build-crop-sheet", json=_payload(2), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["composed_count"] == 2
    assert data["skipped"] == []
    assert data["mime_type"] == "image/png"
    # Decodes to a real PNG (Pillow composed it for real).
    png = base64.b64decode(data["image_base64"])
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert (data["width"], data["height"]) == (img.width, img.height)


def test_build_crop_sheet_all_failed_maps_to_domain_envelope(
    remix_client, auth_headers, monkeypatch
):
    """Every fetch fails → ALL_CROPS_FAILED (RemixDomainError) → spec envelope."""

    async def _boom(url: str):
        raise RuntimeError("network down")

    monkeypatch.setattr(crop_sheet_composer, "fetch_image_bytes", _boom)

    resp = remix_client.post(
        "/api/remix/build-crop-sheet", json=_payload(2), headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "ALL_CROPS_FAILED"


def test_build_crop_sheet_requires_bearer(remix_client):
    resp = remix_client.post("/api/remix/build-crop-sheet", json=_payload(1))
    assert resp.status_code == 401
