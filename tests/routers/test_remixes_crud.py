"""remixes CRUD (specs 02-06) router tests."""

from __future__ import annotations

import uuid


def _seed_snapshot(fake):
    snap_id = uuid.uuid4()
    fake.seed("snapshots", [{"id": snap_id, "book_id": uuid.uuid4()}])
    return snap_id


def _valid_create_body(snapshot_id):
    return {
        "snapshot_id": str(snapshot_id),
        "name": "",
        "remix_config": {"x": 1},
        "illustration": {},
        "characters": [],
        "rmbgs": [{"should": "be ignored"}],
    }


# ---- 02 list ----
def test_list_empty_for_unknown_snapshot(client, fake_adapter, auth_headers):
    r = client.get(f"/api/editor/remixes?snapshot_id={uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["remixes"] == []


def test_list_missing_param_400(client, fake_adapter, auth_headers):
    r = client.get("/api/editor/remixes", headers=auth_headers)
    assert r.status_code == 400


def test_list_bad_uuid_400(client, fake_adapter, auth_headers):
    r = client.get("/api/editor/remixes?snapshot_id=nope", headers=auth_headers)
    assert r.status_code == 400


# ---- 03 get ----
def test_get_not_found_404(client, fake_adapter, auth_headers):
    r = client.get(f"/api/editor/remixes/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404


def test_get_bad_uuid_400(client, fake_adapter, auth_headers):
    r = client.get("/api/editor/remixes/nope", headers=auth_headers)
    assert r.status_code == 400


# ---- 04 create ----
def test_create_normalizes(client, fake_adapter, auth_headers):
    snap = _seed_snapshot(fake_adapter)
    r = client.post("/api/editor/remixes", json=_valid_create_body(snap), headers=auth_headers)
    assert r.status_code == 201
    remix = r.json()["data"]["remix"]
    assert remix["name"] == "New Remix"          # empty -> default
    assert remix["rmbgs"] == [] and remix["upscales"] == []  # job-only forced []
    assert remix["owner_id"] is None
    assert remix["props"] == [] and remix["mixes"] == [] and remix["sprites"] == []


def test_create_snapshot_not_found_422(client, fake_adapter, auth_headers):
    r = client.post("/api/editor/remixes", json=_valid_create_body(uuid.uuid4()), headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"


def test_create_missing_required_400(client, fake_adapter, auth_headers):
    snap = _seed_snapshot(fake_adapter)
    body = {"snapshot_id": str(snap)}  # missing remix_config/illustration/characters
    r = client.post("/api/editor/remixes", json=body, headers=auth_headers)
    assert r.status_code == 400


# ---- 05 update ----
def _seed_remix(fake, snapshot_id=None):
    rid = uuid.uuid4()
    fake.seed("remixes", [{"id": rid, "snapshot_id": snapshot_id or uuid.uuid4(), "mixes": []}])
    return rid


def test_update_writable_ok(client, fake_adapter, auth_headers):
    rid = _seed_remix(fake_adapter)
    r = client.patch(f"/api/editor/remixes/{rid}/columns",
                     json={"columns": {"mixes": [{"a": 1}], "name": "Renamed"}}, headers=auth_headers)
    assert r.status_code == 200
    assert sorted(r.json()["data"]["updated_columns"]) == ["mixes", "name"]


def test_update_remix_config_rejected(client, fake_adapter, auth_headers):
    rid = _seed_remix(fake_adapter)
    r = client.patch(f"/api/editor/remixes/{rid}/columns",
                     json={"columns": {"remix_config": {}}}, headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "COLUMN_NOT_WRITABLE"


def test_update_stage_columns_writable(client, fake_adapter, auth_headers):
    # rmbgs/upscales are client-writable: the FE remix-store owns batch lifecycle
    # (addStageBatch et al.) for all 3 stage columns — jobs only write results.
    rid = _seed_remix(fake_adapter)
    r = client.patch(f"/api/editor/remixes/{rid}/columns",
                     json={"columns": {"rmbgs": [{"id": "b1"}], "upscales": []}},
                     headers=auth_headers)
    assert r.status_code == 200
    assert sorted(r.json()["data"]["updated_columns"]) == ["rmbgs", "upscales"]


def test_update_snapshot_id_rejected(client, fake_adapter, auth_headers):
    rid = _seed_remix(fake_adapter)
    r = client.patch(f"/api/editor/remixes/{rid}/columns",
                     json={"columns": {"snapshot_id": str(uuid.uuid4())}}, headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "COLUMN_NOT_WRITABLE"


def test_update_empty_columns_400(client, fake_adapter, auth_headers):
    rid = _seed_remix(fake_adapter)
    r = client.patch(f"/api/editor/remixes/{rid}/columns", json={"columns": {}}, headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_update_not_found_404(client, fake_adapter, auth_headers):
    r = client.patch(f"/api/editor/remixes/{uuid.uuid4()}/columns",
                     json={"columns": {"name": "x"}}, headers=auth_headers)
    assert r.status_code == 404


# ---- 06 delete ----
def test_delete_idempotent(client, fake_adapter, auth_headers):
    rid = _seed_remix(fake_adapter)
    r1 = client.delete(f"/api/editor/remixes/{rid}", headers=auth_headers)
    assert r1.status_code == 200 and r1.json()["data"]["deleted"] is True
    r2 = client.delete(f"/api/editor/remixes/{rid}", headers=auth_headers)
    assert r2.status_code == 200 and r2.json()["data"]["deleted"] is False


def test_delete_busy_409(client, fake_adapter, auth_headers):
    rid = _seed_remix(fake_adapter)
    fake_adapter.jobs[str(uuid.uuid4())] = {
        "id": uuid.uuid4(), "status": "running", "params": {"remix_id": str(rid)},
    }
    r = client.delete(f"/api/editor/remixes/{rid}", headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REMIX_BUSY"
