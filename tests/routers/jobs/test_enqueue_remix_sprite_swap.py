"""Unit tests for the sprite-swap enqueue route (P3b Phase 06).

Tests the endpoint FUNCTION directly (the central `router.py` wiring is owned
elsewhere) with the in-memory `FakeAppDbAdapter` + an `AsyncMock` for the jobs-lib
`enqueue` (so no runner task spawns) + a stub `resolve_sprite_object_map` (domain
resolver is tested in `tests/services/remix`). Covers: happy 201 shape, 200 dedup,
404 unknown remix.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.auth.editor_session import EditorSessionContext
from src.core.job_types import JOB_TYPE_SPRITE_SWAP
from src.db import adapter as adapter_module
import src.routers.jobs.enqueue_remix_sprite_swap as route_mod
from src.models.jobs.remix_sprite_swap import RemixSpriteSwapEnqueueRequest
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter

_CTX = EditorSessionContext(admin_ref="admin-1", sid="sid-1", consumer=None)


@pytest.fixture
def fake():
    a = FakeAppDbAdapter()
    adapter_module.set_adapter(a)
    yield a
    adapter_module._ADAPTER = None


def _seed_remix(fake: FakeAppDbAdapter) -> tuple[str, str]:
    book_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    remix_id = str(uuid.uuid4())
    sprite_id = str(uuid.uuid4())
    fake.snapshots[snapshot_id] = {"id": snapshot_id, "book_id": book_id, "characters": []}
    fake.remixes[remix_id] = {
        "id": remix_id,
        "snapshot_id": snapshot_id,
        "remix_config": {"characters": []},
        "sprites": [
            {
                "id": sprite_id,
                "crop_sheets": [{"original_crops": [{"object_key": "leela"}], "swap_results": []}],
            }
        ],
    }
    return remix_id, sprite_id


def _stub_pool(monkeypatch):
    pool = SimpleNamespace(lineup=["leela"], missing=[], object_count=1, object_map={})
    monkeypatch.setattr(route_mod, "resolve_sprite_object_map", lambda *a, **k: pool)


def _mock_enqueue(monkeypatch):
    from unittest.mock import AsyncMock

    job = {"id": uuid.uuid4(), "status": "queued"}
    m = AsyncMock(return_value=job)
    monkeypatch.setattr(route_mod, "enqueue", m)
    return m


def _run(coro):
    return asyncio.run(coro)


def test_happy_enqueue_201_shape(fake, monkeypatch):
    remix_id, sprite_id = _seed_remix(fake)
    _stub_pool(monkeypatch)
    m = _mock_enqueue(monkeypatch)

    body = RemixSpriteSwapEnqueueRequest(sprite_id=sprite_id)
    resp = _run(route_mod.enqueue_remix_sprite_swap_endpoint(remix_id, body, _CTX))

    assert resp.status_code == 201
    data = json.loads(resp.body)["data"]
    assert data["type"] == JOB_TYPE_SPRITE_SWAP
    assert data["remix_id"] == remix_id
    assert data["sprite_id"] == sprite_id
    assert data["status"] == "queued"
    assert data["total_steps"] == 1
    assert data["sheets_to_process"] == 1
    assert data["object_count"] == 1
    assert "estimated_duration_sec" in data and "job_id" in data
    # enqueue called with the right type + NO user_id + audit stamped in params.
    assert m.await_args.kwargs["type"] == JOB_TYPE_SPRITE_SWAP
    assert "user_id" not in m.await_args.kwargs
    assert m.await_args.kwargs["params"]["admin_ref"] == "admin-1"
    assert m.await_args.kwargs["params"]["sid"] == "sid-1"


def test_dedup_returns_200(fake, monkeypatch):
    remix_id, sprite_id = _seed_remix(fake)
    _stub_pool(monkeypatch)
    _mock_enqueue(monkeypatch)
    # An active sprite-swap job already exists for this remix.
    fake.jobs["j1"] = {
        "id": "j1",
        "type": JOB_TYPE_SPRITE_SWAP,
        "status": "queued",
        "params": {"remix_id": remix_id, "sprite_id": sprite_id},
    }

    body = RemixSpriteSwapEnqueueRequest(sprite_id=sprite_id)
    resp = _run(route_mod.enqueue_remix_sprite_swap_endpoint(remix_id, body, _CTX))

    # sprite dedup is a plain 200 dict (image-api parity).
    assert isinstance(resp, dict)
    assert resp["data"]["deduped"] is True
    assert resp["data"]["active_swap_key"] == sprite_id


def test_unknown_remix_404(fake, monkeypatch):
    _mock_enqueue(monkeypatch)
    body = RemixSpriteSwapEnqueueRequest(sprite_id=str(uuid.uuid4()))
    with pytest.raises(HTTPException) as exc:
        _run(route_mod.enqueue_remix_sprite_swap_endpoint(str(uuid.uuid4()), body, _CTX))
    assert exc.value.status_code == 404
    assert exc.value.detail["error"]["code"] == "REMIX_NOT_FOUND"
