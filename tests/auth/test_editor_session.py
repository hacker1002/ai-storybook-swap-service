"""Auth verify — 8-case matrix (spec 00). The most important safety layer."""

from __future__ import annotations

import pytest

from scripts.mint_dev_editor_token import mint_token
from src.auth.editor_session import verify_editor_session
from src.core.errors import ServiceError

_SECRET = "test-secret-constant-do-not-reuse"


def _code(header: str | None) -> str:
    with pytest.raises(ServiceError) as ei:
        verify_editor_session(header)
    return ei.value.code


def test_missing_header():
    assert _code(None) == "TOKEN_MISSING"


def test_non_bearer_scheme():
    assert _code("Basic abc") == "TOKEN_MISSING"


def test_malformed_token():
    assert _code("Bearer not-a-jwt") == "TOKEN_INVALID"


def test_wrong_signature():
    tok = mint_token(secret="a-different-secret")
    assert _code(f"Bearer {tok}") == "TOKEN_INVALID"


def test_wrong_audience():
    tok = mint_token(secret=_SECRET, aud="player")
    assert _code(f"Bearer {tok}") == "TOKEN_INVALID"


def test_alg_none_rejected():
    tok = mint_token(secret=_SECRET, alg="none")
    assert _code(f"Bearer {tok}") == "TOKEN_INVALID"


def test_expired():
    tok = mint_token(secret=_SECRET, expired=True)
    assert _code(f"Bearer {tok}") == "TOKEN_EXPIRED"


def test_role_not_admin_forbidden():
    tok = mint_token(secret=_SECRET, role="viewer")
    err = _code(f"Bearer {tok}")
    assert err == "FORBIDDEN"


def test_missing_admin_ref_invalid():
    tok = mint_token(secret=_SECRET, admin_ref="")
    assert _code(f"Bearer {tok}") == "TOKEN_INVALID"


def test_wrong_aud_wins_over_expired():
    # Same secret, wrong aud, AND expired -> aud must be checked first -> INVALID,
    # NOT EXPIRED (regression guard for the PyJWT exp-before-aud ordering).
    tok = mint_token(secret=_SECRET, aud="player", expired=True)
    assert _code(f"Bearer {tok}") == "TOKEN_INVALID"


def test_correct_aud_expired_is_expired():
    tok = mint_token(secret=_SECRET, aud="remix-editor", expired=True)
    assert _code(f"Bearer {tok}") == "TOKEN_EXPIRED"


def test_valid_token_returns_context():
    tok = mint_token(secret=_SECRET, admin_ref="a1", sid="s1", consumer="c1")
    ctx = verify_editor_session(f"Bearer {tok}")
    assert ctx.admin_ref == "a1"
    assert ctx.sid == "s1"
    assert ctx.consumer == "c1"
