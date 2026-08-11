"""Remix column allowlists — the SINGLE source of truth (router + adapter import).

Semantics (ADR-052, spec 04/05):
  - WRITABLE_REMIX_COLUMNS : columns a PATCH /columns may set (whole-column,
    last-writer-wins, no lock).
  - CREATE_ONLY_COLUMNS    : set at INSERT, rejected by PATCH (400 COLUMN_NOT_WRITABLE).
  - JOB_ONLY_COLUMNS       : written only by background jobs; server forces `[]` on
    create and rejects them on PATCH.
"""

from __future__ import annotations

WRITABLE_REMIX_COLUMNS: frozenset[str] = frozenset(
    {"name", "distribution", "illustration", "characters", "props", "mixes", "sprites"}
)

JOB_ONLY_COLUMNS: frozenset[str] = frozenset({"rmbgs", "upscales"})

CREATE_ONLY_COLUMNS: frozenset[str] = frozenset({"remix_config"})

# Columns accepted (and normalized) at INSERT time — a superset of writable +
# create-only + the content columns. `owner_id` is set to NULL by the service.
INSERT_COLUMNS: tuple[str, ...] = (
    "snapshot_id",
    "name",
    "remix_config",
    "illustration",
    "characters",
    "props",
    "mixes",
    "sprites",
    "distribution",
    "rmbgs",
    "upscales",
    "owner_id",
)
