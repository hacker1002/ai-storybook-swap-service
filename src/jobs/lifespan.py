"""Lifespan helpers — `wait_all` drains in-flight handler tasks during shutdown.

Ported verbatim from `ai-storybook-image-api/src/jobs/lifespan.py` (no DB — reads
the runner's strong-ref `_TASKS` set only).
"""

from __future__ import annotations

import asyncio
import logging

from src.jobs.runner import _TASKS

logger = logging.getLogger(__name__)


async def wait_all(timeout: float | None = None) -> None:
    """Await all in-flight jobs, with optional timeout. Never raises.

    Snapshot `_TASKS` before awaiting — the set mutates as tasks finish
    (done_callback discards them).
    """
    pending = [t for t in _TASKS if not t.done()]
    if not pending:
        logger.info("wait_all_noop pending=0")
        return

    logger.info("wait_all_start pending=%d timeout=%s", len(pending), timeout)
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
        logger.info("wait_all_done pending=0")
    except asyncio.TimeoutError:
        still = [t for t in pending if not t.done()]
        logger.warning("wait_all_timeout still_pending=%d", len(still))
