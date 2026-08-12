"""POST /internal/auth/revoke (spec 00 §3) — S2S guard + denylist write."""

from __future__ import annotations

import pytest

from src.auth import session_stores

_RV = "/internal/auth/revoke"
_KEY = {"X-API-Key": "test-internal-key"}


def test_revoke_by_sid(client):
    r = client.post(_RV, json={"sid": "s1"}, headers=_KEY)
    assert r.status_code == 200
    assert r.json() == {"success": True}
    assert session_stores.is_revoked("s1", "a-any") is True


def test_revoke_by_admin_ref(client):
    r = client.post(_RV, json={"admin_ref": "a1"}, headers=_KEY)
    assert r.status_code == 200
    assert session_stores.is_revoked("any-sid", "a1") is True


def test_idempotent(client):
    assert client.post(_RV, json={"sid": "s1"}, headers=_KEY).status_code == 200
    assert client.post(_RV, json={"sid": "s1"}, headers=_KEY).status_code == 200


def test_missing_key_unauthorized(client):
    r = client.post(_RV, json={"sid": "s1"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "API_KEY_INVALID"


def test_wrong_key_unauthorized(client):
    r = client.post(_RV, json={"sid": "s1"}, headers={"X-API-Key": "nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "API_KEY_INVALID"


def test_empty_body_validation_error(client):
    r = client.post(_RV, json={}, headers=_KEY)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_fail_closed_when_key_unconfigured(client, monkeypatch):
    from src.config.settings import settings

    monkeypatch.setattr(settings, "internal_api_key", "")
    # even a "correct" empty key is rejected — fail-closed, 401 not 500
    r = client.post(_RV, json={"sid": "s1"}, headers=_KEY)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "API_KEY_INVALID"
