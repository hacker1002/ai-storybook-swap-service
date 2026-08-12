"""POST /api/editor/auth/exchange (spec 00 §1) — handoff -> access token + round-trip."""

from __future__ import annotations

import pytest

from scripts.mint_dev_editor_token import mint_handoff_assertion
from src.services import ip_rate_limit

_HANDOFF_SECRET = "test-handoff-secret-do-not-reuse"

_EX = "/api/editor/auth/exchange"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    ip_rate_limit.reset_for_test()
    yield
    ip_rate_limit.reset_for_test()


def _mint(**kw) -> str:
    return mint_handoff_assertion(secret=_HANDOFF_SECRET, **kw)


def test_happy_path_flat_body(client):
    r = client.post(_EX, json={"code": _mint()})
    assert r.status_code == 200
    body = r.json()
    assert "success" not in body  # FLAT — no {success,data} envelope
    assert body["expires_in"] == 43200
    assert isinstance(body["access_token"], str) and body["access_token"]
    assert r.headers["cache-control"] == "no-store"


def test_replay_rejected(client):
    a = _mint()
    assert client.post(_EX, json={"code": a}).status_code == 200
    r = client.post(_EX, json={"code": a})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "HANDOFF_INVALID"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secret_override": True},  # wrong secret
        {"expired": True},
        {"aud": "remix-editor"},  # wrong aud
        {"alg": "none"},
    ],
)
def test_invalid_assertions_all_handoff_invalid(client, kwargs):
    if kwargs.pop("secret_override", False):
        code = mint_handoff_assertion(secret="a-wrong-secret")
    else:
        code = _mint(**kwargs)
    r = client.post(_EX, json={"code": code})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "HANDOFF_INVALID"


def test_ttl_clamp_rejects_long_assertion(client):
    # exp - iat = 3600s >> 60s -> HANDOFF_INVALID even though signature/aud are valid.
    r = client.post(_EX, json={"code": _mint(ttl=3600)})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "HANDOFF_INVALID"


def test_missing_code_validation_error(client):
    r = client.post(_EX, json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_admin_name_echoed_and_sanitized(client):
    r = client.post(_EX, json={"code": _mint(admin_name="Nguyen A\x00\x07")})
    assert r.status_code == 200
    assert r.json()["admin_name"] == "Nguyen A"  # control chars stripped


def test_rate_limit(client, monkeypatch):
    from src.config.settings import settings

    monkeypatch.setattr(settings, "auth_exchange_rate_limit_per_min", 2)
    # 2 allowed (invalid assertions still count as requests), 3rd -> 429
    for _ in range(2):
        client.post(_EX, json={"code": "not-a-jwt"})
    r = client.post(_EX, json={"code": "not-a-jwt"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"


def test_exchanged_token_passes_editor_auth_then_revoked(client, fake_adapter):
    a = _mint(jti="known-jti-123")
    tok = client.post(_EX, json={"code": a}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    book = "/api/editor/book-bundle/00000000-0000-4000-8000-000000000000"
    # auth passes (404 = book missing in fake adapter, NOT 401)
    assert client.get(book, headers=headers).status_code == 404
    # revoke the session (sid == jti) then the same token is rejected
    rv = client.post("/internal/auth/revoke", json={"sid": "known-jti-123"}, headers={"X-API-Key": "test-internal-key"})
    assert rv.status_code == 200
    r = client.get(book, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_INVALID"
