"""list-actors (spec 10) router tests — casting resolve phía App."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from src.main import app


def _actor(snapshot_id, **over) -> dict:
    row = {
        "id": uuid.uuid4(),
        "snapshot_id": snapshot_id,
        "owner_id": uuid.uuid4(),
        "actant_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "actor_type": "human",
        "mixes": {},
        "rmbgs": {},
        "upscales": {},
    }
    row.update(over)
    return row


def test_list_actors_returns_rows_for_snapshot(client, fake_adapter, auth_headers):
    snap = uuid.uuid4()
    other = uuid.uuid4()
    fake_adapter.seed("actors", [_actor(snap), _actor(snap), _actor(other)])
    r = client.get(f"/api/editor/actors?snapshot_id={snap}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert len(body["data"]["actors"]) == 2


def test_list_actors_unknown_snapshot_empty_200(client, fake_adapter, auth_headers):
    r = client.get(f"/api/editor/actors?snapshot_id={uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["actors"] == []


def test_list_actors_missing_param_400(client, fake_adapter, auth_headers):
    r = client.get("/api/editor/actors", headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_actors_bad_uuid_400(client, fake_adapter, auth_headers):
    r = client.get("/api/editor/actors?snapshot_id=nope", headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_actors_requires_auth(client, fake_adapter):
    r = client.get(f"/api/editor/actors?snapshot_id={uuid.uuid4()}")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_MISSING"


def test_list_actors_db_error_500(fake_adapter, auth_headers):
    # A non-raising client so the catch-all Exception handler's 500 envelope is
    # observed instead of the TestClient re-raising (raise_server_exceptions default).
    fake_adapter.fail_on("list_actors", RuntimeError("boom"))
    # No `with` -> lifespan skipped (real asyncpg pool never opened); the fake adapter
    # injected by the fixture is the seam.
    safe_client = TestClient(app, raise_server_exceptions=False)
    r = safe_client.get(f"/api/editor/actors?snapshot_id={uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"


def test_list_actors_returns_full_row_unfiltered(client, fake_adapter, auth_headers):
    # A pipeline-incomplete row (no upscales) must still come back whole — the FE
    # reads batch state to disable presets; the service never filters.
    snap = uuid.uuid4()
    incomplete = _actor(snap)
    del incomplete["upscales"]
    fake_adapter.seed("actors", [incomplete])
    r = client.get(f"/api/editor/actors?snapshot_id={snap}", headers=auth_headers)
    assert r.status_code == 200
    actors = r.json()["data"]["actors"]
    assert len(actors) == 1
    assert "upscales" not in actors[0]
