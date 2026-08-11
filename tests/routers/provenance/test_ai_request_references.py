"""GET /api/provenance/ai-request-references/{id} — existence-check authz (P3c Gap 2).

Uses the injected FakeAppDbAdapter (no DB). Verifies the ref_files mapping, the
index-before-filter gap rule, and the 404 / 400 / 401 outcomes.
"""

from __future__ import annotations

import uuid

from src.routers.provenance import get_ai_request_references as prov

_ID = "11111111-1111-1111-1111-111111111111"


def _log_row(ref_files) -> dict:
    return {
        "id": _ID,
        "operation": "retouch.edit_object_image",
        "provider": "gemini",
        "model": "gemini-3-pro-image",
        "status": "success",
        "created_at": "2026-08-11T00:00:00Z",
        "book_id": None,
        "snapshot_id": None,
        "remix_id": None,
        "request": {"ref_files": ref_files},
    }


def test_happy(client, auth_headers, fake_adapter):
    fake_adapter.seed(
        "ai_logs",
        [_log_row([
            {"url": "https://cdn.test/ref1.png", "mime": "image/png", "bytes": 100},
            {"url": "https://cdn.test/ref2.png"},
        ])],
    )
    resp = client.get(f"/api/provenance/ai-request-references/{_ID}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["aiRequestId"] == _ID
    assert body["data"]["provider"] == "gemini"
    imgs = body["data"]["images"]
    assert [i["index"] for i in imgs] == [1, 2]
    assert imgs[0]["mimeType"] == "image/png"
    assert body["meta"] == {"totalRefFiles": 2, "skippedCount": 0}


def test_skips_urlless_entry_but_keeps_index(client, auth_headers, fake_adapter):
    # middle entry has no url → dropped + counted, indexes stay 1 and 3.
    fake_adapter.seed(
        "ai_logs",
        [_log_row([
            {"url": "https://cdn.test/a.png"},
            {"sha256": "deadbeef"},  # upload-failed, no url
            {"url": "https://cdn.test/c.png"},
        ])],
    )
    resp = client.get(f"/api/provenance/ai-request-references/{_ID}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["index"] for i in body["data"]["images"]] == [1, 3]
    assert body["meta"] == {"totalRefFiles": 3, "skippedCount": 1}


def test_not_found_404(client, auth_headers, fake_adapter):
    other = str(uuid.uuid4())
    resp = client.get(f"/api/provenance/ai-request-references/{other}", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_non_uuid_400(client, auth_headers, fake_adapter):
    resp = client.get("/api/provenance/ai-request-references/not-a-uuid", headers=auth_headers)
    assert resp.status_code == 400, resp.text


def test_requires_bearer(client, fake_adapter):
    resp = client.get(f"/api/provenance/ai-request-references/{_ID}")
    assert resp.status_code == 401


def test_map_ref_files_defensive_non_dict():
    # unit: a garbage `request` shape yields ([],0,0), never a 500.
    assert prov.map_ref_files(None) == ([], 0, 0)
    assert prov.map_ref_files({"ref_files": "nope"}) == ([], 0, 0)
    assert prov.map_ref_files({}) == ([], 0, 0)
