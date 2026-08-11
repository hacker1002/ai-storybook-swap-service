"""job-status (spec 07) router tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _seed_job(fake, **over):
    jid = over.get("id", uuid.uuid4())
    row = {
        "id": jid,
        "type": "remix_mix_swap",
        "status": "running",
        "step_details": {"s": 1},
        "result": None,
        "error_message": "boom",
        "cancel_requested": False,
        "params": {"remix_id": "r1", "batch_id": "b1"},
        "book_id": uuid.uuid4(),
        "current_step": 2,
        "total_steps": 5,
        "updated_at": datetime(2026, 8, 11, tzinfo=timezone.utc),
    }
    row.update(over)
    fake.jobs[str(jid)] = row
    return jid


def test_status_jobs_and_missing(client, fake_adapter, auth_headers):
    jid = _seed_job(fake_adapter)
    missing = uuid.uuid4()
    r = client.get(f"/api/jobs/status?ids={jid},{missing}", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["jobs"]) == 1
    assert data["missing"] == [str(missing)]
    entry = data["jobs"][0]
    assert entry["error"] == "boom"                 # error_message -> error
    assert entry["params"]["batch_id"] == "b1"      # additive field present
    assert entry["current_step"] == 2 and entry["total_steps"] == 5


def test_status_empty_ids_400(client, fake_adapter, auth_headers):
    r = client.get("/api/jobs/status?ids=", headers=auth_headers)
    assert r.status_code == 400


def test_status_too_many_ids_400(client, fake_adapter, auth_headers):
    ids = ",".join(str(uuid.uuid4()) for _ in range(21))
    r = client.get(f"/api/jobs/status?ids={ids}", headers=auth_headers)
    assert r.status_code == 400


def test_status_bad_uuid_400(client, fake_adapter, auth_headers):
    r = client.get("/api/jobs/status?ids=not-a-uuid", headers=auth_headers)
    assert r.status_code == 400


def test_status_requires_auth(client, fake_adapter):
    r = client.get(f"/api/jobs/status?ids={uuid.uuid4()}")
    assert r.status_code == 401


def test_status_dedupes_ids(client, fake_adapter, auth_headers):
    jid = _seed_job(fake_adapter)
    r = client.get(f"/api/jobs/status?ids={jid},{jid}", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["data"]["jobs"]) == 1
    assert r.json()["data"]["missing"] == []
