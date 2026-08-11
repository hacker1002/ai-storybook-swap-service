"""Unit tests for the DETECT enqueue routes (jobs 11/12/13).

Endpoint functions are driven DIRECTLY (async) with a hand-built editor session +
`fake_adapter` seam; the module-level `enqueue` is monkeypatched to an AsyncMock so
no background job task spawns. Covers: 201 enqueue shape, dedup divergence (sprite
job 11 → 200 deduped; mix job 12 + rmbg job 13 → 409 JOB_ALREADY_ACTIVE), and
unknown-remix → 404. Registry reset is LOCAL (autouse) so importing the route
modules (which import handler modules → `@register`) can't leak across test files.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from src.auth.editor_session import EditorSessionContext
from src.jobs import runner
from src.routers.jobs import (
    enqueue_remix_detect_defects as r_sprite,
    enqueue_remix_detect_mix_defects as r_mix,
    enqueue_remix_detect_rmbg_defects as r_rmbg,
)
from src.services.remix.errors import RemixDomainError

REMIX_ID = "11111111-1111-1111-1111-111111111111"
BATCH_ID = "22222222-2222-2222-2222-222222222222"
SPRITE_ID = "55555555-5555-5555-5555-555555555555"
SNAP_ID = "33333333-3333-3333-3333-333333333333"
BOOK_ID = "44444444-4444-4444-4444-444444444444"

SESSION = EditorSessionContext(admin_ref="admin-1", sid="sid-1", consumer=None)


@pytest.fixture(autouse=True)
def _preserve_registry():
    reg = dict(runner._REGISTRY)
    fin = dict(runner._FINALIZE_HOOKS)
    yield
    runner._REGISTRY.clear()
    runner._REGISTRY.update(reg)
    runner._FINALIZE_HOOKS.clear()
    runner._FINALIZE_HOOKS.update(fin)


def _selected_sheet() -> dict:
    return {
        "sheet_geometry": {"width": 1024, "height": 768},
        "original_crops": [],
        "swap_results": [{"is_selected": True, "media_url": "http://x/r.png", "crops": []}],
    }


def _seed_snapshot(fake):
    fake.snapshots[SNAP_ID] = {"id": SNAP_ID, "book_id": BOOK_ID, "characters": [], "props": []}


def _active_job(job_type: str) -> dict:
    return {
        "id": uuid.uuid4(),
        "type": job_type,
        "status": "queued",
        "params": {"remix_id": REMIX_ID, "batch_id": BATCH_ID, "sprite_id": SPRITE_ID},
    }


def _body_json(resp):
    return json.loads(resp.body)


# ─── detect-rmbg (job 13) — simplest: full 201 / 409 / 404 ────────────────────


async def test_detect_rmbg_enqueue_201(fake_adapter, monkeypatch):
    fake_adapter.remixes[REMIX_ID] = {
        "id": REMIX_ID, "snapshot_id": SNAP_ID,
        "rmbgs": [{"id": BATCH_ID, "crop_sheets": [_selected_sheet(), _selected_sheet()]}],
    }
    _seed_snapshot(fake_adapter)
    job_id = uuid.uuid4()

    async def _fake_enqueue(**kwargs):
        _fake_enqueue.kwargs = kwargs
        return {"id": job_id}

    monkeypatch.setattr(r_rmbg, "enqueue", _fake_enqueue)

    body = r_rmbg.RemixDetectRmbgDefectsEnqueueRequest(batch_id=BATCH_ID)
    resp = await r_rmbg.enqueue_remix_detect_rmbg_defects_endpoint(REMIX_ID, body, SESSION)

    assert resp.status_code == 201
    data = _body_json(resp)["data"]
    assert data["type"] == "remix_detect_rmbg_defects"
    assert data["remix_id"] == REMIX_ID
    assert data["batch_id"] == BATCH_ID
    assert data["total_steps"] == 2 and data["sheets_to_process"] == 2
    # enqueue wiring: type + audit stamped into params.
    assert _fake_enqueue.kwargs["type"] == "remix_detect_rmbg_defects"
    assert _fake_enqueue.kwargs["params"]["admin_ref"] == "admin-1"
    assert _fake_enqueue.kwargs["params"]["sid"] == "sid-1"


async def test_detect_rmbg_enqueue_dedup_409(fake_adapter):
    fake_adapter.remixes[REMIX_ID] = {
        "id": REMIX_ID, "snapshot_id": SNAP_ID,
        "rmbgs": [{"id": BATCH_ID, "crop_sheets": [_selected_sheet()]}],
    }
    _seed_snapshot(fake_adapter)
    fake_adapter.jobs["dup"] = _active_job("remix_detect_rmbg_defects")

    body = r_rmbg.RemixDetectRmbgDefectsEnqueueRequest(batch_id=BATCH_ID)
    with pytest.raises(RemixDomainError) as ei:
        await r_rmbg.enqueue_remix_detect_rmbg_defects_endpoint(REMIX_ID, body, SESSION)
    assert ei.value.status == 409 and ei.value.code == "JOB_ALREADY_ACTIVE"


async def test_detect_rmbg_enqueue_unknown_remix_404(fake_adapter):
    body = r_rmbg.RemixDetectRmbgDefectsEnqueueRequest(batch_id=BATCH_ID)
    with pytest.raises(RemixDomainError) as ei:
        await r_rmbg.enqueue_remix_detect_rmbg_defects_endpoint(REMIX_ID, body, SESSION)
    assert ei.value.status == 404 and ei.value.code == "REMIX_NOT_FOUND"


# ─── detect-mix (job 12) — 201 + 409 divergence ──────────────────────────────


async def test_detect_mix_enqueue_201(fake_adapter, monkeypatch):
    fake_adapter.remixes[REMIX_ID] = {
        "id": REMIX_ID, "snapshot_id": SNAP_ID, "characters": [], "props": [], "sprites": [],
        "mixes": [{"id": BATCH_ID, "crop_sheets": [_selected_sheet()]}],
    }
    _seed_snapshot(fake_adapter)
    monkeypatch.setattr(
        r_mix, "resolve_mix_swap_context",
        lambda *a, **k: SimpleNamespace(swap_targets=[{"key": "hero"}], target_count=1),
    )
    job_id = uuid.uuid4()
    monkeypatch.setattr(r_mix, "enqueue", lambda **kw: _returns({"id": job_id}))

    body = r_mix.RemixDetectMixDefectsEnqueueRequest(batch_id=BATCH_ID)
    resp = await r_mix.enqueue_remix_detect_mix_defects_endpoint(REMIX_ID, body, SESSION)

    assert resp.status_code == 201
    data = _body_json(resp)["data"]
    assert data["type"] == "remix_detect_mix_defects"
    assert data["target_count"] == 1


async def test_detect_mix_enqueue_dedup_409(fake_adapter, monkeypatch):
    fake_adapter.remixes[REMIX_ID] = {
        "id": REMIX_ID, "snapshot_id": SNAP_ID, "characters": [], "props": [], "sprites": [],
        "mixes": [{"id": BATCH_ID, "crop_sheets": [_selected_sheet()]}],
    }
    _seed_snapshot(fake_adapter)
    monkeypatch.setattr(
        r_mix, "resolve_mix_swap_context",
        lambda *a, **k: SimpleNamespace(swap_targets=[{"key": "hero"}], target_count=1),
    )
    fake_adapter.jobs["dup"] = _active_job("remix_detect_mix_defects")

    body = r_mix.RemixDetectMixDefectsEnqueueRequest(batch_id=BATCH_ID)
    with pytest.raises(RemixDomainError) as ei:
        await r_mix.enqueue_remix_detect_mix_defects_endpoint(REMIX_ID, body, SESSION)
    assert ei.value.status == 409 and ei.value.code == "JOB_ALREADY_ACTIVE"


# ─── detect-sprite (job 11) — dedup DIVERGES to 200 (not 409) ─────────────────


async def test_detect_sprite_enqueue_dedup_200(fake_adapter, monkeypatch):
    fake_adapter.remixes[REMIX_ID] = {
        "id": REMIX_ID, "snapshot_id": SNAP_ID, "remix_config": {"characters": []},
        "sprites": [{"id": SPRITE_ID, "crop_sheets": [_selected_sheet()]}],
    }
    _seed_snapshot(fake_adapter)
    monkeypatch.setattr(
        r_sprite, "resolve_sprite_object_map",
        lambda *a, **k: SimpleNamespace(lineup=["hero"], missing=[], object_count=1, object_map={}),
    )
    fake_adapter.jobs["dup"] = _active_job("remix_detect_defects")

    body = r_sprite.RemixDetectDefectsEnqueueRequest(sprite_id=SPRITE_ID)
    result = await r_sprite.enqueue_remix_detect_defects_endpoint(REMIX_ID, body, SESSION)

    # DIVERGENCE: job 11 returns a plain 200 dict (deduped:true), NOT a 409.
    assert isinstance(result, dict)
    assert result["data"]["deduped"] is True
    assert result["data"]["active_swap_key"] == SPRITE_ID


async def test_detect_sprite_enqueue_unknown_remix_404(fake_adapter):
    body = r_sprite.RemixDetectDefectsEnqueueRequest(sprite_id=SPRITE_ID)
    with pytest.raises(RemixDomainError) as ei:
        await r_sprite.enqueue_remix_detect_defects_endpoint(REMIX_ID, body, SESSION)
    assert ei.value.status == 404 and ei.value.code == "REMIX_NOT_FOUND"


class _Awaitable:
    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _c():
            return self._value
        return _c().__await__()


def _returns(value):
    return _Awaitable(value)
