"""GET /api/jobs/status?ids= (spec 07) — batch job-status polling.

NEW endpoint (image-api has none) replacing Supabase realtime for the sub-app.
Batch semantics: unknown ids go into `missing[]`, never 404 the whole request. Cap
20 ids (query-bomb guard). No ownership filter (role-wide session) — an id you
can't see is indistinguishable from a non-existent one (both -> missing).
Rate limit: DEFERRED (validation 260811).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Query

from src.core.errors import validation_error
from src.db.adapter import get_adapter
from src.models.jobs.status import map_job_row

_MAX_IDS = 20


def _parse_ids(raw: str) -> list[UUID]:
    """Split comma-separated ids, strip, dedupe (order preserved), validate 1..20 UUID."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise validation_error("ids must contain at least 1 UUID")
    # Bound work early (GET has no body cap) — reject before parsing N UUIDs.
    if len(parts) > _MAX_IDS:
        raise validation_error(f"at most {_MAX_IDS} ids allowed")
    seen: set[str] = set()
    ordered: list[UUID] = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        try:
            ordered.append(UUID(p))
        except (ValueError, AttributeError):
            raise validation_error(f"'{p}' is not a valid UUID")
    if len(ordered) > _MAX_IDS:
        raise validation_error(f"at most {_MAX_IDS} ids allowed")
    return ordered


async def get_job_status(ids: str = Query(...)) -> dict:
    parsed = _parse_ids(ids)
    rows = await get_adapter().get_jobs(parsed)

    found = {str(r["id"]) for r in rows}
    jobs = [map_job_row(r) for r in rows]
    missing = [str(i) for i in parsed if str(i) not in found]
    return {"success": True, "data": {"jobs": jobs, "missing": missing}}
