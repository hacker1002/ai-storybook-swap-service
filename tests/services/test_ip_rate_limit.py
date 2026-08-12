"""Per-IP sliding-window rate limiter."""

from __future__ import annotations

import pytest

from src.services import ip_rate_limit


@pytest.fixture(autouse=True)
def _reset():
    ip_rate_limit.reset_for_test()
    yield
    ip_rate_limit.reset_for_test()


def test_within_limit():
    assert all(ip_rate_limit.check_ip("1.1.1.1", limit=3) for _ in range(3))


def test_over_limit_blocked():
    for _ in range(3):
        ip_rate_limit.check_ip("1.1.1.1", limit=3)
    assert ip_rate_limit.check_ip("1.1.1.1", limit=3) is False


def test_independent_per_ip():
    for _ in range(3):
        ip_rate_limit.check_ip("1.1.1.1", limit=3)
    assert ip_rate_limit.check_ip("2.2.2.2", limit=3) is True


def test_window_slides(monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(ip_rate_limit.time, "monotonic", lambda: t["v"])
    for _ in range(3):
        ip_rate_limit.check_ip("1.1.1.1", limit=3, window=60)
    assert ip_rate_limit.check_ip("1.1.1.1", limit=3, window=60) is False
    t["v"] += 61  # old entries fall out of the window
    assert ip_rate_limit.check_ip("1.1.1.1", limit=3, window=60) is True


def test_zero_limit_disables():
    assert all(ip_rate_limit.check_ip("1.1.1.1", limit=0) for _ in range(100))
