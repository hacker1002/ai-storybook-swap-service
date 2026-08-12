"""Mint access token — claims/TTL + round-trip through the real verifier."""

from __future__ import annotations

import jwt

from src.auth.editor_session import verify_editor_session
from src.auth.mint_editor_token import mint_access_token

_SECRET = "test-secret-constant-do-not-reuse"


def test_claims_and_ttl():
    token, expires_in = mint_access_token("admin-1", sid="sid-1", consumer="c")
    assert expires_in == 43200
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"], audience="remix-editor")
    assert claims["role"] == "admin"
    assert claims["admin_ref"] == "admin-1"
    assert claims["sid"] == "sid-1"
    assert claims["consumer"] == "c"
    assert claims["exp"] - claims["iat"] == 43200


def test_consumer_omitted_when_none():
    token, _ = mint_access_token("admin-1", sid="sid-1")
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"], audience="remix-editor")
    assert "consumer" not in claims


def test_round_trip_through_verifier():
    token, _ = mint_access_token("admin-1", sid="sid-1", consumer="dev")
    ctx = verify_editor_session(f"Bearer {token}")
    assert ctx.admin_ref == "admin-1"
    assert ctx.sid == "sid-1"
    assert ctx.consumer == "dev"


def test_mint_uses_last_secret_verify_accepts_rotation(monkeypatch):
    """Mint signs with the newest secret; verify still accepts a token minted while
    an old secret is also configured (rotation window)."""
    from src.config import settings as settings_module

    monkeypatch.setattr(
        type(settings_module.settings),
        "editor_token_secrets",
        property(lambda self: ["old-secret", _SECRET]),
    )
    token, _ = mint_access_token("a", sid="s")
    # signed with [-1] == _SECRET (which the verifier tries in the list) -> passes
    ctx = verify_editor_session(f"Bearer {token}")
    assert ctx.admin_ref == "a"
