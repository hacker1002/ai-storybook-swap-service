"""Unit tests for the audio-swap enqueue route (P3b Phase 06).

Endpoint FUNCTION tested directly with `FakeAppDbAdapter` + `AsyncMock` enqueue.
The precheck is pure logic over `illustration` (no domain resolver to stub).
Covers: happy 201 shape, 200 dedup, 404 unknown remix. (The audio-swap HANDLER —
which pulls in the ElevenLabs/narration stack — is covered separately.)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.auth.editor_session import EditorSessionContext
from src.core.job_types import JOB_TYPE_AUDIO_SWAP
from src.db import adapter as adapter_module
import src.routers.jobs.enqueue_remix_audio_swap as route_mod
from src.models.jobs.remix_audio_swap import RemixAudioSwapEnqueueRequest
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter

_CTX = EditorSessionContext(admin_ref="admin-1", sid="sid-1", consumer=None)


@pytest.fixture
def fake():
    a = FakeAppDbAdapter()
    adapter_module.set_adapter(a)
    yield a
    adapter_module._ADAPTER = None


def _seed_remix(fake: FakeAppDbAdapter) -> str:
    book_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    remix_id = str(uuid.uuid4())
    fake.snapshots[snapshot_id] = {"id": snapshot_id, "book_id": book_id}
    fake.remixes[remix_id] = {
        "id": remix_id,
        "snapshot_id": snapshot_id,
        "remix_config": {"languages": [{"code": "en_US", "is_enabled": True}]},
        "illustration": {
            "spreads": [
                {
                    "id": "sp1",
                    "textboxes": [
                        {
                            "id": "tb1",
                            # script_synced=False → needs_regen True.
                            "en_US": {"audio": {"chunks": [{"script_synced": False, "voice_id": "v1"}]}},
                        }
                    ],
                }
            ]
        },
    }
    return remix_id


def _mock_enqueue(monkeypatch) -> AsyncMock:
    m = AsyncMock(return_value={"id": uuid.uuid4(), "status": "queued"})
    monkeypatch.setattr(route_mod, "enqueue", m)
    return m


def _run(coro):
    return asyncio.run(coro)


def test_happy_enqueue_201_shape(fake, monkeypatch):
    remix_id = _seed_remix(fake)
    m = _mock_enqueue(monkeypatch)

    body = RemixAudioSwapEnqueueRequest(triggered_by="user")
    resp = _run(route_mod.enqueue_remix_audio_swap_endpoint(remix_id, body, _CTX))

    assert resp.status_code == 201
    data = json.loads(resp.body)["data"]
    assert data["type"] == JOB_TYPE_AUDIO_SWAP
    assert data["remix_id"] == remix_id
    assert data["total_steps"] == 1
    assert data["chunks_to_regen"] == 1
    assert data["textboxes_to_recombine"] == 1
    assert data["skipped"] is False
    assert m.await_args.kwargs["type"] == JOB_TYPE_AUDIO_SWAP
    assert "user_id" not in m.await_args.kwargs
    assert m.await_args.kwargs["params"]["admin_ref"] == "admin-1"


def test_dedup_returns_200(fake, monkeypatch):
    remix_id = _seed_remix(fake)
    _mock_enqueue(monkeypatch)
    fake.jobs["j1"] = {
        "id": "j1",
        "type": JOB_TYPE_AUDIO_SWAP,
        "status": "queued",
        "params": {"remix_id": remix_id},
    }

    body = RemixAudioSwapEnqueueRequest(triggered_by="user")
    resp = _run(route_mod.enqueue_remix_audio_swap_endpoint(remix_id, body, _CTX))

    assert isinstance(resp, dict)
    assert resp["data"]["deduped"] is True
    assert resp["data"]["type"] == JOB_TYPE_AUDIO_SWAP


def test_unknown_remix_404(fake, monkeypatch):
    _mock_enqueue(monkeypatch)
    body = RemixAudioSwapEnqueueRequest(triggered_by="user")
    with pytest.raises(HTTPException) as exc:
        _run(route_mod.enqueue_remix_audio_swap_endpoint(str(uuid.uuid4()), body, _CTX))
    assert exc.value.status_code == 404
    assert exc.value.detail["error"]["code"] == "REMIX_NOT_FOUND"
