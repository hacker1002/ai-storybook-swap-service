"""Per-IP sliding-window rate limiter for the public exchange endpoint.

Guards `POST /api/editor/auth/exchange` — the ONE endpoint with no Bearer — against
brute-forcing handoff assertions. Sync + in-memory (a dict of per-IP deques of
monotonic timestamps), O(1) amortised.

SINGLE-PROCESS ONLY (same constraint as `session_stores`): with `workers=N` the
effective cap is N × limit and resets on restart. Behind a proxy every request may
carry the proxy's IP in `request.client.host` — handling `X-Forwarded-For` is a
deploy-topology follow-up, deliberately NOT done here (trusting XFF blindly lets a
client spoof its own bucket). Acceptable at current single-worker deploy.
"""

from __future__ import annotations

import time
from collections import deque

_WINDOW_DEFAULT = 60.0
_buckets: dict[str, deque[float]] = {}


def check_ip(ip: str, limit: int, window: float = _WINDOW_DEFAULT) -> bool:
    """Return True if the request is within budget (and record it), False if the IP
    has hit `limit` requests inside `window` seconds. `limit <= 0` disables the guard."""
    if limit <= 0:
        return True
    now = time.monotonic()
    cutoff = now - window
    bucket = _buckets.setdefault(ip, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def reset_for_test() -> None:
    _buckets.clear()
