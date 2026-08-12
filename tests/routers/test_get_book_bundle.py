"""book-bundle (spec 01) router tests."""

from __future__ import annotations

import uuid


def _seed_book(fake, *, with_snapshot=True, artstyle_id=None):
    # `artstyle_id` kept as a param so we can prove the bundle returns artStyle:null
    # even when a legacy Editor-schema book still carries the (now-dropped) column.
    book_id = uuid.uuid4()
    snap_id = uuid.uuid4()
    fake.seed("books", [{"id": book_id, "current_version": snap_id if with_snapshot else None,
                         "artstyle_id": artstyle_id, "title": "T"}])
    if with_snapshot:
        fake.seed("snapshots", [{"id": snap_id, "book_id": book_id, "version": "v1",
                                 "illustration": {"a": 1}}])
    return book_id, snap_id


def test_bundle_ok_five_blocks(client, fake_adapter, auth_headers):
    book_id, snap_id = _seed_book(fake_adapter)
    fake_adapter.seed("humans", [{"id": uuid.uuid4(), "display_name": {}}])
    fake_adapter.seed("voices", [{"id": uuid.uuid4(), "name": "v"}])
    r = client.get(f"/api/editor/book-bundle/{book_id}", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["contractVersion"] == 1
    assert data["book"]["id"] == str(book_id)
    assert data["snapshot"]["id"] == str(snap_id)
    assert data["artStyle"] is None
    assert len(data["humans"]) == 1 and len(data["voices"]) == 1


def test_bundle_art_style_always_null(client, fake_adapter, auth_headers):
    # App DB rev 2 drops books.artstyle_id; artStyle is a constant null regardless of
    # whether a legacy book row still has the column set.
    book_no_col, _ = _seed_book(fake_adapter, artstyle_id=None)
    book_with_col, _ = _seed_book(fake_adapter, artstyle_id=uuid.uuid4())
    for book_id in (book_no_col, book_with_col):
        r = client.get(f"/api/editor/book-bundle/{book_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["data"]["artStyle"] is None


def test_bundle_book_not_found(client, fake_adapter, auth_headers):
    r = client.get(f"/api/editor/book-bundle/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_bundle_snapshot_missing_404(client, fake_adapter, auth_headers):
    book_id, _ = _seed_book(fake_adapter, with_snapshot=False)
    # current_version None + no snapshots for book -> 404
    r = client.get(f"/api/editor/book-bundle/{book_id}", headers=auth_headers)
    assert r.status_code == 404


def test_bundle_requires_auth(client, fake_adapter):
    book_id, _ = _seed_book(fake_adapter)
    r = client.get(f"/api/editor/book-bundle/{book_id}")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_MISSING"


def test_bundle_bad_uuid_400(client, fake_adapter, auth_headers):
    r = client.get("/api/editor/book-bundle/not-a-uuid", headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
