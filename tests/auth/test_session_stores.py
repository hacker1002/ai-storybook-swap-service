"""In-memory session stores — one-time jti + denylist TTL/idempotency (ADR-053)."""

from __future__ import annotations

import pytest

from src.auth import session_stores


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(session_stores, "time", c)
    session_stores.reset_stores_for_test()
    return c


def test_jti_one_time(clock):
    assert session_stores.mark_jti_used("j1") is True
    assert session_stores.mark_jti_used("j1") is False  # replay


def test_jti_reusable_after_ttl(clock):
    assert session_stores.mark_jti_used("j1") is True
    clock.t += session_stores.JTI_TTL + 1  # entry expired + swept
    # store-level the jti is free again; the exchange guard relies on the assertion
    # ALSO being expired by now (its exp <= iat + 60s) — see test_exchange.
    assert session_stores.mark_jti_used("j1") is True


def test_revoke_by_sid(clock):
    assert session_stores.is_revoked("s1", "a1") is False
    session_stores.revoke(sid="s1")
    assert session_stores.is_revoked("s1", "a1") is True
    assert session_stores.is_revoked("s2", "a1") is False  # other sid unaffected


def test_revoke_by_admin_ref_blocks_all_sids(clock):
    session_stores.revoke(admin_ref="a1")
    assert session_stores.is_revoked("any-sid", "a1") is True
    assert session_stores.is_revoked("any-sid", "a2") is False


def test_revoke_idempotent(clock):
    session_stores.revoke(sid="s1")
    session_stores.revoke(sid="s1")  # no raise, still revoked
    assert session_stores.is_revoked("s1", "a1") is True


def test_denylist_expires_after_ttl(clock):
    session_stores.revoke(sid="s1")
    assert session_stores.is_revoked("s1", "a1") is True
    clock.t += session_stores.DENY_TTL + 1
    assert session_stores.is_revoked("s1", "a1") is False


def test_sweep_removes_expired_jti(clock):
    session_stores.mark_jti_used("old")
    clock.t += session_stores.JTI_TTL + 1
    clock.t += session_stores.SWEEP_EVERY  # ensure sweep is due
    session_stores.mark_jti_used("new")  # write path triggers sweep
    assert "old" not in session_stores._used_jti


def test_reset_clears_all(clock):
    session_stores.mark_jti_used("j1")
    session_stores.revoke(sid="s1", admin_ref="a1")
    session_stores.reset_stores_for_test()
    assert session_stores.mark_jti_used("j1") is True
    assert session_stores.is_revoked("s1", "a1") is False
