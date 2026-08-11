"""Unit tests for the mix-swap enqueue route (P3b Phase 06).

Endpoint FUNCTION tested directly with `FakeAppDbAdapter` + `AsyncMock` enqueue +
a stub `resolve_mix_swap_context`. Covers: happy 201 shape, 200 dedup (image-api
parity — the Phase-06 409 divergence was reverted 260811), 404 unknown remix.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.auth.editor_session import EditorSessionContext
from src.core.job_types import JOB_TYPE_MIX_SWAP
from src.db import adapter as adapter_module
import src.routers.jobs.enqueue_remix_mix_swap as route_mod
from src.models.jobs.remix_mix_swap import RemixMixSwapEnqueueRequest
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
    batch_id = str(uuid.uuid4())
    fake.snapshots[snapshot_id] = {
        "id": snapshot_id, "book_id": book_id, "characters": [], "props": []
    }
    fake.remixes[remix_id] = {
        "id": remix_id,
        "snapshot_id": snapshot_id,
        "remix_config": {},
        "characters": [],
        "props": [],
        "sprites": [],
        "mixes": [
            {
                "id": batch_id,
                "crop_sheets": [{"original_crops": [{"id": "c1", "tags": []}], "swap_results": []}],
            }
        ],
    }
    return remix_id, batch_id


def _stub_ctx(monkeypatch):
    ctx = SimpleNamespace(missing_char_refs=[], swap_targets=["leela"], target_count=1)
    monkeypatch.setattr(route_mod, "resolve_mix_swap_context", lambda *a, **k: ctx)


def _mock_enqueue(monkeypatch) -> AsyncMock:
    m = AsyncMock(return_value={"id": uuid.uuid4(), "status": "queued"})
    monkeypatch.setattr(route_mod, "enqueue", m)
    return m


def _run(coro):
    return asyncio.run(coro)


def test_happy_enqueue_201_shape(fake, monkeypatch):
    remix_id, batch_id = _seed_remix(fake)
    _stub_ctx(monkeypatch)
    m = _mock_enqueue(monkeypatch)

    body = RemixMixSwapEnqueueRequest(batch_id=batch_id)
    resp = _run(route_mod.enqueue_remix_mix_swap_endpoint(remix_id, body, _CTX))

    assert resp.status_code == 201
    data = json.loads(resp.body)["data"]
    assert data["type"] == JOB_TYPE_MIX_SWAP
    assert data["remix_id"] == remix_id
    assert data["batch_id"] == batch_id
    assert data["target_count"] == 1
    assert data["total_steps"] == 1
    assert data["sheets_to_process"] == 1
    assert m.await_args.kwargs["type"] == JOB_TYPE_MIX_SWAP
    assert "user_id" not in m.await_args.kwargs
    assert m.await_args.kwargs["params"]["admin_ref"] == "admin-1"


def test_dedup_returns_200_deduped(fake, monkeypatch):
    remix_id, batch_id = _seed_remix(fake)
    _stub_ctx(monkeypatch)
    m = _mock_enqueue(monkeypatch)
    fake.jobs["j1"] = {
        "id": "j1",
        "type": JOB_TYPE_MIX_SWAP,
        "status": "running",
        "params": {"remix_id": remix_id, "batch_id": batch_id},
    }

    body = RemixMixSwapEnqueueRequest(batch_id=batch_id)
    resp = _run(route_mod.enqueue_remix_mix_swap_endpoint(remix_id, body, _CTX))
    # Image-api parity (spec jobs/05 §Dedup Response) — no new row created.
    assert resp == {
        "success": True,
        "data": {
            "deduped": True,
            "job_id": "j1",
            "status": "running",
            "type": JOB_TYPE_MIX_SWAP,
            "remix_id": remix_id,
            "active_swap_key": batch_id,
        },
    }
    m.assert_not_awaited()


def test_unknown_remix_404(fake, monkeypatch):
    _mock_enqueue(monkeypatch)
    body = RemixMixSwapEnqueueRequest(batch_id=str(uuid.uuid4()))
    with pytest.raises(HTTPException) as exc:
        _run(route_mod.enqueue_remix_mix_swap_endpoint(str(uuid.uuid4()), body, _CTX))
    assert exc.value.status_code == 404
    assert exc.value.detail["error"]["code"] == "REMIX_NOT_FOUND"
