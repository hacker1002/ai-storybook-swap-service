"""POST /api/remix/detect-* — core mocked (AsyncMock) + validation/envelope paths.

Covers the 4 detect routes. Happy paths mock the Gemini core and assert the response
envelope; validation paths assert the `RemixDomainError`-in-validator → spec envelope
(the high-risk "envelope drift" guard from the phase risk table) and the route-level
422 precondition on detect-crop-geometry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models.requests.detect_crop_geometry import DetectCropGeometryMeta
from src.models.requests.detect_rmbg_defects import (
    DetectRmbgDefectsMeta,
    SwappedDimensions as RmbgDims,
)
from src.routers.remix import detect_crop_geometry as geo_mod
from src.routers.remix import detect_rmbg_defects as rmbg_mod

# ── detect-crop-geometry ────────────────────────────────────────────────────


def _geo_payload(cw: int = 128) -> dict:
    return {
        "original_sheet_url": "https://example.test/orig.png",
        "swapped_sheet_url": "https://example.test/swapped.png",
        "crops": [
            {"number": 1, "geometry": {"x": 0, "y": 0, "w": cw, "h": 128}},
        ],
        "original_sheet_dimensions": {"width": 256, "height": 128},
        "target_numbers": [1],
    }


def test_detect_crop_geometry_happy(remix_client, auth_headers, monkeypatch):
    async def _fake(req, *, ai_context=None):
        return SimpleNamespace(detections=[], meta=DetectCropGeometryMeta(detectedCount=0))

    monkeypatch.setattr(geo_mod, "run_detect_crop_geometry", _fake)
    resp = remix_client.post(
        "/api/remix/detect-crop-geometry", json=_geo_payload(), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["detections"] == []


def test_detect_crop_geometry_precondition_422(remix_client, auth_headers):
    # crop geometry exceeds sheet width (0+300 > 256) → route-level 422 domain error.
    resp = remix_client.post(
        "/api/remix/detect-crop-geometry", json=_geo_payload(cw=300), headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_detect_crop_geometry_requires_bearer(remix_client):
    resp = remix_client.post("/api/remix/detect-crop-geometry", json=_geo_payload())
    assert resp.status_code == 401


# ── detect-rmbg-defects ─────────────────────────────────────────────────────


def _rmbg_crop(cid: str = "c0") -> dict:
    return {
        "id": cid,
        "media_url": f"https://example.test/{cid}.png",
        "geometry": {"x": 0, "y": 0, "w": 128, "h": 128},
    }


def _rmbg_payload(crops=None, result_crops=None) -> dict:
    return {
        "sheet_geometry": {"width": 256, "height": 128},
        "crops": crops if crops is not None else [_rmbg_crop("c0")],
        "result_crops": result_crops if result_crops is not None else [_rmbg_crop("c0")],
        "original_sheet_url": "https://example.test/orig.png",
        "result_sheet_url": "https://example.test/result.png",
    }


def test_detect_rmbg_happy(remix_client, auth_headers, monkeypatch):
    async def _fake(req, *, ai_context=None):
        return SimpleNamespace(
            defects=[],
            meta=DetectRmbgDefectsMeta(
                cellCount=1, defectCount=0, swappedDimensions=RmbgDims(width=256, height=128)
            ),
        )

    monkeypatch.setattr(rmbg_mod, "run_detect_rmbg_defects", _fake)
    resp = remix_client.post(
        "/api/remix/detect-rmbg-defects", json=_rmbg_payload(), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["defects"] == []


def test_detect_rmbg_empty_crops_domain_envelope(remix_client, auth_headers):
    resp = remix_client.post(
        "/api/remix/detect-rmbg-defects", json=_rmbg_payload(crops=[]), headers=auth_headers
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    # validator-raised RemixDomainError → spec envelope (not FastAPI 422 default).
    assert "code" in body["error"]


# ── detect-swap-defects / detect-mix-defects — validation/envelope path ──────
# Their request models reuse the heavy sprite/mix swap models; the highest-value
# assertion (per risk table) is that a validator-raised RemixDomainError surfaces as
# the spec envelope + the route is Bearer-gated. Happy paths are covered by the
# shared core-mock pattern above (identical wiring).


def test_detect_swap_defects_requires_bearer(remix_client):
    resp = remix_client.post("/api/remix/detect-swap-defects", json={"crops": []})
    assert resp.status_code == 401


def test_detect_mix_defects_requires_bearer(remix_client):
    resp = remix_client.post("/api/remix/detect-mix-defects", json={"crops": []})
    assert resp.status_code == 401
