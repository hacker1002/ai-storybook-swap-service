"""In-memory per-operation rate limiter for ElevenLabs upstream calls.

Each logical operation (key) has an independent sliding-window budget of
`ELEVEN_MAX_REQUESTS_PER_SECOND` requests per 1-second window. GET-style
calls (voice metadata lookups) bypass — they are cheap reads that do not
consume generation quota.

Design:
  - Per-key bucket: `tts`, `music`, `sound_effect`, etc. each get their
    own 5 req/s budget. A spike on music compose does not starve TTS.
  - Sliding window via deque of monotonic timestamps. O(1) amortised.
  - `acquire_eleven_slot(key)` awaits until a slot frees up; does NOT raise.
    Callers are serialised at the acquire point (per-key asyncio.Lock) so
    order is FIFO within a key.
  - GETs do not call this module.

Trade-offs (acknowledged):
  - Single-process. With `uvicorn --workers N`, effective per-key limit is
    N × cap. Acceptable for current single-worker deployment; revisit when
    scaling out.
  - Resets on restart.
  - No backpressure ceiling: callers may queue unbounded if inbound rate
    exceeds cap for sustained periods. Combine with per-user `check_rate_limit`
    (see `src/services/rate_limit.py`) to bound depth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

logger = logging.getLogger(__name__)

ELEVEN_MAX_REQUESTS_PER_SECOND = int(
    os.getenv("ELEVEN_MAX_REQUESTS_PER_SECOND", "5")
)
_WINDOW_S = 1.0

_buckets: dict[str, deque[float]] = {}
_locks: dict[str, asyncio.Lock] = {}
_registry_lock = asyncio.Lock()


async def _get_lock(key: str) -> asyncio.Lock:
    async with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


async def acquire_eleven_slot(key: str) -> None:
    """Block until a request slot is available for the given operation key.

    Call before every non-GET ElevenLabs HTTP request, with a key identifying
    the logical operation (e.g. "tts", "music", "sound_effect"). Each key
    has an independent budget.
    """
    if ELEVEN_MAX_REQUESTS_PER_SECOND <= 0:
        return

    lock = await _get_lock(key)
    async with lock:
        bucket = _buckets.setdefault(key, deque())
        while True:
            now = time.monotonic()
            cutoff = now - _WINDOW_S
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) < ELEVEN_MAX_REQUESTS_PER_SECOND:
                bucket.append(now)
                return

            wait_s = bucket[0] + _WINDOW_S - now
            if wait_s > 0:
                logger.debug(
                    "elevenlabs_rate_limit_wait key=%s wait_s=%.3f depth=%d cap=%d",
                    key, wait_s, len(bucket), ELEVEN_MAX_REQUESTS_PER_SECOND,
                )
                await asyncio.sleep(wait_s)
