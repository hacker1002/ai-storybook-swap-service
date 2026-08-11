"""Route tests for the P3b STAGE (rmbg/upscale) + DETECT enqueue/cancel endpoints.

Self-contained: builds a FastAPI app mounting ONLY the ported routers under the
`/api/jobs` group with the real `require_editor_session` dep (so the central
`router.py` wiring is not needed here). The DB seam is the conftest `fake_adapter`;
the spawned handler task is neutralized by a LOCAL registry stub (autouse) so
`enqueue` never runs a real AI core. Distinct file name (`test_stage_detect_*`) to
avoid collision with the peer's swap/audio/mix test files.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from src.auth.editor_session import require_editor_session
from src.core.errors import register_exception_handlers
from src.routers.remix.error_handler import remix_domain_error_handler
from src.services.remix.errors import RemixDomainError

# Importing the route modules also registers the handlers (side effect).
from src.routers.jobs import (  # noqa: E402
    cancel_job,
    enqueue_remix_detect_defects,
    enqueue_remix_detect_mix_defects,
    enqueue_remix_detect_rmbg_defects,
    enqueue_remix_rmbg,
    enqueue_remix_upscale,
)

_STAGE_DETECT_TYPES = [
    "remix_rmbg",
    "remix_upscale",
    "remix_detect_defects",
    "remix_detect_mix_defects",
    "remix_detect_rmbg_defects",
]


@pytest.fixture(autouse=True)
def _stub_job_handlers():
    """Neutralize the runner registry for the 5 job types so `enqueue`'s spawned
    task calls a no-op (never a real core). LOCAL — restores after each test."""
    from src.jobs import runner

    saved = dict(runner._REGISTRY)

    async def _noop(job, ctx):  # noqa: ANN001
        return ("completed", {})

    for t in _STAGE_DETECT_TYPES:
        runner._REGISTRY[t] = _noop
    yield
    runner._REGISTRY.clear()
    runner._REGISTRY.update(saved)


@pytest.fixture
def jobs_client(fake_adapter) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_exception_handler(RemixDomainError, remix_domain_error_handler)
    group = APIRouter(prefix="/api/jobs", dependencies=[Depends(require_editor_session)])
    for mod in (
        enqueue_remix_rmbg,
        enqueue_remix_upscale,
        enqueue_remix_detect_defects,
        enqueue_remix_detect_mix_defects,
        enqueue_remix_detect_rmbg_defects,
        cancel_job,
    ):
        group.include_router(mod.router)
    app.include_router(group)
    return TestClient(app)


def _seed_remix(fake_adapter, *, column: str, batch_id: str, sheet: dict) -> str:
    remix_id = str(uuid.uuid4())
    snap_id = str(uuid.uuid4())
    fake_adapter.snapshots[snap_id] = {"id": snap_id, "book_id": str(uuid.uuid4())}
    fake_adapter.remixes[remix_id] = {
        "id": remix_id,
        "snapshot_id": snap_id,
        column: [{"id": batch_id, "crop_sheets": [sheet]}],
    }
    return remix_id


_STAGE_SHEET = {
    "original_crops": [
        {"id": "c0", "spread_id": "s0", "media_url": "http://x/a.png",
         "geometry": {"x": 0, "y": 0, "w": 10, "h": 10}}
    ],
    "swap_results": [],
}
_SELECTED_SHEET = {
    "original_crops": [
        {"id": "c0", "spread_id": "s0", "media_url": "http://x/a.png",
         "geometry": {"x": 0, "y": 0, "w": 10, "h": 10}}
    ],
    "swap_results": [{"is_selected": True, "media_url": "http://x/sel.png", "crops": []}],
}


# ─── STAGE: rmbg / upscale ────────────────────────────────────────────────────


def test_rmbg_enqueue_201(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="rmbgs", batch_id="b1", sheet=_STAGE_SHEET)
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/rmbg", json={"batch_id": "b1"}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["type"] == "remix_rmbg"
    assert data["remix_id"] == remix_id
    assert data["batch_id"] == "b1"
    assert data["status"] == "queued"
    assert data["sheets_to_process"] == 1 and data["total_steps"] == 1
    # audit admin_ref/sid stamped into the persisted params.
    job = list(fake_adapter.jobs.values())[0]
    assert job["params"]["admin_ref"] and job["params"]["sid"]
    assert job["params"]["model_params"]["model"] == "bria/remove-background"


def test_upscale_enqueue_201_default_model(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="upscales", batch_id="b1", sheet=_STAGE_SHEET)
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/upscale", json={"batch_id": "b1"}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["type"] == "remix_upscale"
    job = list(fake_adapter.jobs.values())[0]
    assert job["params"]["model_params"]["model"] == "xinntao/realesrgan"


def test_upscale_unsupported_model_422(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="upscales", batch_id="b1", sheet=_STAGE_SHEET)
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/upscale",
        json={"batch_id": "b1", "model_params": {"model": "nope/not-real"}},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_MODEL"
    assert err["details"]["model"] == "nope/not-real"


def test_stage_unknown_remix_404(jobs_client, fake_adapter, auth_headers):
    resp = jobs_client.post(
        f"/api/jobs/remix/{uuid.uuid4()}/rmbg", json={"batch_id": "b1"}, headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REMIX_NOT_FOUND"


def test_stage_skipped_no_crop_sheets(jobs_client, fake_adapter, auth_headers):
    remix_id = str(uuid.uuid4())
    snap_id = str(uuid.uuid4())
    fake_adapter.snapshots[snap_id] = {"id": snap_id, "book_id": str(uuid.uuid4())}
    fake_adapter.remixes[remix_id] = {
        "id": remix_id, "snapshot_id": snap_id, "rmbgs": [{"id": "b1", "crop_sheets": []}],
    }
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/rmbg", json={"batch_id": "b1"}, headers=auth_headers
    )
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert d["skipped"] is True and d["reason"] == "no_crop_sheets"


def test_stage_dedup_200(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="rmbgs", batch_id="b1", sheet=_STAGE_SHEET)
    active = str(uuid.uuid4())
    fake_adapter.jobs[active] = {
        "id": active, "type": "remix_rmbg", "status": "running",
        "params": {"remix_id": remix_id, "batch_id": "b1"},
    }
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/rmbg", json={"batch_id": "b1"}, headers=auth_headers
    )
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert d["deduped"] is True and d["job_id"] == active and d["active_key"] == "b1"


# ─── cancel ───────────────────────────────────────────────────────────────────


def test_cancel_flips_flag(jobs_client, fake_adapter, auth_headers):
    jid = str(uuid.uuid4())
    fake_adapter.jobs[jid] = {"id": jid, "status": "running", "cancel_requested": False}
    resp = jobs_client.post(f"/api/jobs/{jid}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["cancel_requested"] is True
    assert fake_adapter.jobs[jid]["cancel_requested"] is True


def test_cancel_terminal_is_noop(jobs_client, fake_adapter, auth_headers):
    jid = str(uuid.uuid4())
    fake_adapter.jobs[jid] = {"id": jid, "status": "completed", "cancel_requested": False}
    resp = jobs_client.post(f"/api/jobs/{jid}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert d["cancel_requested"] is False and d["note"] == "job_already_terminal"


def test_cancel_unknown_404(jobs_client, fake_adapter, auth_headers):
    resp = jobs_client.post(f"/api/jobs/{uuid.uuid4()}/cancel", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


# ─── DETECT: rmbg (409 dedup) / mix (409) / sprite (200) ──────────────────────


def test_detect_rmbg_enqueue_201(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="rmbgs", batch_id="b1", sheet=_SELECTED_SHEET)
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/detect-rmbg-defects",
        json={"batch_id": "b1"}, headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["type"] == "remix_detect_rmbg_defects"


def test_detect_rmbg_no_result_422(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="rmbgs", batch_id="b1", sheet=_STAGE_SHEET)
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/detect-rmbg-defects",
        json={"batch_id": "b1"}, headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NO_RMBG_RESULT"


def test_detect_rmbg_dedup_409(jobs_client, fake_adapter, auth_headers):
    remix_id = _seed_remix(fake_adapter, column="rmbgs", batch_id="b1", sheet=_SELECTED_SHEET)
    active = str(uuid.uuid4())
    fake_adapter.jobs[active] = {
        "id": active, "type": "remix_detect_rmbg_defects", "status": "queued",
        "params": {"remix_id": remix_id, "batch_id": "b1"},
    }
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/detect-rmbg-defects",
        json={"batch_id": "b1"}, headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "JOB_ALREADY_ACTIVE"
    assert err["details"]["job_id"] == active


def test_detect_mix_dedup_409(jobs_client, fake_adapter, auth_headers, monkeypatch):
    # Stub the target-pool resolver so the route reaches the dedup step.
    class _Ctx:
        swap_targets = [{"key": "hero"}]
        target_count = 1
    monkeypatch.setattr(
        enqueue_remix_detect_mix_defects, "resolve_mix_swap_context", lambda *a, **k: _Ctx()
    )
    remix_id = _seed_remix(fake_adapter, column="mixes", batch_id="b1", sheet=_SELECTED_SHEET)
    active = str(uuid.uuid4())
    fake_adapter.jobs[active] = {
        "id": active, "type": "remix_detect_mix_defects", "status": "running",
        "params": {"remix_id": remix_id, "batch_id": "b1"},
    }
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/detect-mix-defects",
        json={"batch_id": "b1"}, headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "JOB_ALREADY_ACTIVE"


def test_detect_sprite_dedup_200(jobs_client, fake_adapter, auth_headers, monkeypatch):
    # Stub the sprite object-pool resolver so the route reaches the dedup step.
    class _Pool:
        lineup = ["hero"]
        missing = []
        object_count = 1
    monkeypatch.setattr(
        enqueue_remix_detect_defects, "resolve_sprite_object_map", lambda *a, **k: _Pool()
    )
    remix_id = str(uuid.uuid4())
    snap_id = str(uuid.uuid4())
    fake_adapter.snapshots[snap_id] = {"id": snap_id, "book_id": str(uuid.uuid4()), "characters": []}
    fake_adapter.remixes[remix_id] = {
        "id": remix_id, "snapshot_id": snap_id, "remix_config": {"characters": []},
        "sprites": [{"id": "sp1", "crop_sheets": [_SELECTED_SHEET]}],
    }
    active = str(uuid.uuid4())
    fake_adapter.jobs[active] = {
        "id": active, "type": "remix_detect_defects", "status": "queued",
        "params": {"remix_id": remix_id, "sprite_id": "sp1"},
    }
    resp = jobs_client.post(
        f"/api/jobs/remix/{remix_id}/detect-sprite-defects",
        json={"sprite_id": "sp1"}, headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["data"]
    assert d["deduped"] is True and d["active_swap_key"] == "sp1"
