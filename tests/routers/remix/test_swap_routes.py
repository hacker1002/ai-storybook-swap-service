"""POST /api/remix/swap-sprite-sheet + /swap-mix-crop-sheet — core mocked (AsyncMock).

The Gemini cores (`run_swap_sprite_sheet`, `run_swap_mix_sheet`) are replaced so the
tests assert transport wiring (request → core, core result → response envelope + meta)
and the `RemixDomainError` → spec-envelope path, without any real AI/Storage I/O.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.routers.remix import swap_mix_crop_sheet as mix_mod
from src.routers.remix import swap_sprite_sheet as sprite_mod
from src.services.remix.errors import RemixDomainError


def _sprite_payload() -> dict:
    return {
        "sheet_geometry": {"width": 256, "height": 128},
        "crops": [
            {
                "type": "character",
                "object_key": "hero",
                "variant_key": "v1",
                "media_url": "https://example.test/c0.png",
                "geometry": {"x": 0, "y": 0, "w": 128, "h": 128},
            }
        ],
        "swap_objects": [
            {
                "object_key": "hero",
                "human_image_url": "https://example.test/hero.png",
                "swap_traits": [{"type": "face", "description": "swap the face"}],
                "object_context": {"name": "Hero"},
            }
        ],
    }


def _mix_payload() -> dict:
    return {
        "sheet_geometry": {"width": 256, "height": 128},
        "crops": [
            {
                "id": "c0",
                "media_url": "https://example.test/c0.png",
                "geometry": {"x": 0, "y": 0, "w": 128, "h": 128},
            }
        ],
        "swap_targets": [
            {
                "key": "hero",
                "reference_image_url": "https://example.test/ref.png",
                "object_context": {},
            }
        ],
    }


def _sprite_result() -> SimpleNamespace:
    return SimpleNamespace(
        image_url="https://cdn.test/out.png",
        width=256,
        height=128,
        token_usage=1234,
        composed_sheet_url="https://cdn.test/composed.png",
        ai_request_id="rid-abc",
        compose_ms=10,
        gemini_ms=20,
        upload_ms=5,
        object_count=1,
        cell_count=1,
        payload_bytes_sheet=111,
        payload_bytes_humans=222,
        swapped_object_keys=["hero"],
    )


def _mix_result() -> SimpleNamespace:
    return SimpleNamespace(
        image_url="https://cdn.test/mix.png",
        width=256,
        height=128,
        token_usage=999,
        composed_sheet_url="https://cdn.test/mixcomposed.png",
        ai_request_id="rid-mix",
        variant_sheet_urls=None,
        compose_ms=11,
        gemini_ms=22,
        upload_ms=6,
        target_count=1,
        targets_with_base=0,
        payload_bytes_sheet=1,
        payload_bytes_variant_old=0,
        payload_bytes_variant_new=0,
        skipped_references=[],
    )


def test_swap_sprite_happy(remix_client, auth_headers, monkeypatch):
    async def _fake(body, *, ai_context=None):
        # remixId (None here) flows to the cost bucket via ai_context.
        assert ai_context is not None
        return _sprite_result()

    monkeypatch.setattr(sprite_mod, "run_swap_sprite_sheet", _fake)
    resp = remix_client.post(
        "/api/remix/swap-sprite-sheet", json=_sprite_payload(), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["image_url"] == "https://cdn.test/out.png"
    assert body["data"]["aiRequestId"] == "rid-abc"
    assert body["meta"]["objectCount"] == 1
    assert body["meta"]["swappedObjects"] == ["hero"]


def test_swap_sprite_domain_error_envelope(remix_client, auth_headers, monkeypatch):
    async def _boom(body, *, ai_context=None):
        raise RemixDomainError(
            status=422, code="REFERENCE_IMAGE_MISSING", message="no ref"
        )

    monkeypatch.setattr(sprite_mod, "run_swap_sprite_sheet", _boom)
    resp = remix_client.post(
        "/api/remix/swap-sprite-sheet", json=_sprite_payload(), headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "REFERENCE_IMAGE_MISSING"


def test_swap_mix_happy(remix_client, auth_headers, monkeypatch):
    async def _fake(body, *, ai_context=None):
        return _mix_result()

    monkeypatch.setattr(mix_mod, "run_swap_mix_sheet", _fake)
    resp = remix_client.post(
        "/api/remix/swap-mix-crop-sheet", json=_mix_payload(), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["image_url"] == "https://cdn.test/mix.png"
    assert body["meta"]["targetCount"] == 1


def test_swap_mix_requires_bearer(remix_client):
    resp = remix_client.post("/api/remix/swap-mix-crop-sheet", json=_mix_payload())
    assert resp.status_code == 401
