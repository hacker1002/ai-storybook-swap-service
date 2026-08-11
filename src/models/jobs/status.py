"""Job-status response mapping (spec 07 + validation-260811 additive fields).

`JobStatusEntry` shape mirrors the realtime row the FE mapper reads
(map-background-job-row.ts: params.remix_id|batch_id|sprite_id|character_key|
triggered_by, result.defectsBySheet) so the sub-app reuses that mapper unchanged.
DB column `error_message` is surfaced as `error`.
"""

from __future__ import annotations

from datetime import datetime, timezone


# Service-stamped audit fields — internal attribution, not part of the FE mapper
# contract (routes on remix_id|batch_id|sprite_id|...). `sid` is a live session id;
# never surface it through the polling response.
_PARAMS_AUDIT_KEYS = frozenset({"admin_ref", "sid"})


def _project_params(params) -> dict | None:
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if k not in _PARAMS_AUDIT_KEYS}


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(value)


def map_job_row(row: dict) -> dict:
    """DB background_jobs row -> JobStatusEntry (additive fields included)."""
    return {
        "id": str(row["id"]),
        "type": row["type"],
        "status": row["status"],
        "step_details": row.get("step_details"),
        "result": row.get("result"),
        "error": row.get("error_message"),  # column rename
        "cancel_requested": bool(row.get("cancel_requested", False)),
        "updated_at": _iso(row.get("updated_at")),
        # additive (validation 260811) — FE mapper routes on these
        "params": _project_params(row.get("params")),
        "book_id": str(row["book_id"]) if row.get("book_id") else None,
        "current_step": row.get("current_step"),
        "total_steps": row.get("total_steps"),
    }
