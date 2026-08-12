"""In-memory session state — one-time jti + editor-session denylist (ADR-053).

SINGLE-PROCESS ONLY. All state is module-level dicts, lost on restart. This is a
deliberate ADR-053 trade-off: the service MUST run with `workers=1` (see
`scripts/run-service.sh` + the `WEB_CONCURRENCY` boot guard in `src/main.py`). With
N workers each has an independent copy → a jti could be exchanged N times and a
revoke would only hit one worker. Scaling out REQUIRES moving this to Redis/DB
FIRST (additive; the verify/exchange contract does not change).

Two stores:
  - `_used_jti`: one-time enforcement for handoff assertions (jti -> expires_at).
    TTL 60s = the assertion's own max lifetime; after that the assertion is `exp`
    anyway so the entry can be swept without reopening a replay window.
  - denylist `_revoked_sids` / `_revoked_admin_refs`: revoked sessions (value ->
    expires_at). TTL 12h = the access-token ceiling; a token cannot outlive its
    denylist entry.

Sweep is periodic (every SWEEP_EVERY on the WRITE path only) so `is_revoked` — hit
on every authenticated request — stays a pair of O(1) dict lookups with no sweep.
"""

from __future__ import annotations

import time

# Mirrors handoff TTL (assertion max lifetime) and access-token ceiling.
JTI_TTL = 60
DENY_TTL = 43200  # 12h — must be >= editor_access_token_ttl_seconds
SWEEP_EVERY = 60

_used_jti: dict[str, float] = {}
_revoked_sids: dict[str, float] = {}
_revoked_admin_refs: dict[str, float] = {}
_last_sweep: float = 0.0


def _sweep_if_due(now: float) -> None:
    """Drop expired entries from all three stores, at most once per SWEEP_EVERY.
    Called only on the write paths (mark_jti_used / revoke) — never on the hot
    read path — so a large denylist never taxes request latency."""
    global _last_sweep
    if now - _last_sweep < SWEEP_EVERY:
        return
    for store in (_used_jti, _revoked_sids, _revoked_admin_refs):
        for key in [k for k, exp in store.items() if exp <= now]:
            del store[key]
    _last_sweep = now


def mark_jti_used(jti: str) -> bool:
    """Atomically check-and-set one-time use. Returns False if `jti` was already
    consumed and not yet expired (replay), True on first use.

    MUST stay a single synchronous function with NO `await` between the read and the
    write: the event loop cannot preempt sync code, so this is race-free on one
    process. Do not split it or make it async — that would reintroduce a TOCTOU
    replay window.
    """
    now = time.time()
    _sweep_if_due(now)
    if _used_jti.get(jti, 0.0) > now:
        return False
    _used_jti[jti] = now + JTI_TTL
    return True


def revoke(*, sid: str | None = None, admin_ref: str | None = None) -> None:
    """Add a session/admin to the denylist. Idempotent (re-revoke refreshes TTL)."""
    now = time.time()
    _sweep_if_due(now)
    expires_at = now + DENY_TTL
    if sid:
        _revoked_sids[sid] = expires_at
    if admin_ref:
        _revoked_admin_refs[admin_ref] = expires_at


def is_revoked(sid: str, admin_ref: str) -> bool:
    """Hot path — O(1), NO sweep. True if either the session or its admin is denied."""
    now = time.time()
    return _revoked_sids.get(sid, 0.0) > now or _revoked_admin_refs.get(admin_ref, 0.0) > now


def reset_stores_for_test() -> None:
    """Clear all module state. Called by an autouse fixture — module-level state
    would otherwise leak a 'used' jti / revoked sid across tests (flaky)."""
    global _last_sweep
    _used_jti.clear()
    _revoked_sids.clear()
    _revoked_admin_refs.clear()
    _last_sweep = 0.0
