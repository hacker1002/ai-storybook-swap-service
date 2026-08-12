"""Remix column allowlists — the SINGLE source of truth (router + adapter import).

Semantics (ADR-052, spec 04/05):
  - WRITABLE_REMIX_COLUMNS : columns a PATCH /columns may set (whole-column,
    last-writer-wins, no lock). Includes all 3 stage-pipeline columns
    (`mixes`/`rmbgs`/`upscales`) — the FE remix-store owns batch LIFECYCLE
    client-side (addStageBatch/removeStageBatch/importStageBatch/relayout/
    takeFinalBack persist the whole stage column), while job handlers write
    batch RESULTS into the same columns via the separate
    `update_remix_job_column` seam. Races are gated FE-side
    (useAnySwapRunning + enqueue dedup) — parity with the main editor.
  - CREATE_ONLY_COLUMNS    : set at INSERT, rejected by PATCH (400 COLUMN_NOT_WRITABLE).
  - JOB_ONLY_COLUMNS       : the subset job handlers may write through
    `update_remix_job_column` (NOT disjoint from WRITABLE — see above);
    server still forces `[]` for these on create.
"""

from __future__ import annotations

WRITABLE_REMIX_COLUMNS: frozenset[str] = frozenset(
    {
        "name",
        "distribution",
        "illustration",
        "characters",
        "props",
        "mixes",
        "rmbgs",
        "upscales",
        "sprites",
    }
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
