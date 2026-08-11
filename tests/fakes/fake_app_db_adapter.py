"""In-memory AppDbAdapter for deterministic router/unit tests (no DB).

Scriptable: `seed(table, rows)`, `fail_on(method, exc)`. It mirrors the Protocol —
`test_fake_implements_protocol` asserts conformance so a signature drift fails fast
rather than silently diverging from Postgres behavior.

Live smoke (Phase 07 step 9) still catches what the fake cannot: real column names,
JSONB round-trip, FK constraints.
"""

from __future__ import annotations

from uuid import UUID

from src.core.remix_columns import JOB_ONLY_COLUMNS, WRITABLE_REMIX_COLUMNS


class FakeAppDbAdapter:
    def __init__(self) -> None:
        self.books: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.art_styles: dict[str, dict] = {}
        self.humans: list[dict] = []
        self.voices: list[dict] = []
        self.remixes: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        # keyed by `name` (the prompt_templates key column), NOT `id` — seed via
        # `fake.prompt_templates[name] = {...}` directly (the generic `seed()` helper
        # keys dicts by `id`, which does not apply here).
        self.prompt_templates: dict[str, dict] = {}
        self._fail: dict[str, Exception] = {}

    # ---- scripting helpers ----
    def seed(self, table: str, rows) -> None:
        target = getattr(self, table)
        if isinstance(target, list):
            target.extend(rows)
        else:
            for r in rows:
                target[str(r["id"])] = r

    def fail_on(self, method: str, exc: Exception) -> None:
        self._fail[method] = exc

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail:
            raise self._fail[method]

    # ---- reads ----
    async def get_book(self, book_id: UUID) -> dict | None:
        self._maybe_fail("get_book")
        return self.books.get(str(book_id))

    async def get_current_snapshot(self, book_id: UUID, current_version: UUID | None) -> dict | None:
        self._maybe_fail("get_current_snapshot")
        if current_version is not None:
            return self.snapshots.get(str(current_version))
        matches = [s for s in self.snapshots.values() if str(s.get("book_id")) == str(book_id)]
        return matches[-1] if matches else None

    async def get_snapshot(self, snapshot_id: UUID) -> dict | None:
        self._maybe_fail("get_snapshot")
        return self.snapshots.get(str(snapshot_id))

    async def get_art_style(self, art_style_id: UUID) -> dict | None:
        self._maybe_fail("get_art_style")
        return self.art_styles.get(str(art_style_id))

    async def list_humans(self, book_id: UUID) -> list[dict]:
        self._maybe_fail("list_humans")
        return list(self.humans)

    async def list_voices(self, book_id: UUID) -> list[dict]:
        self._maybe_fail("list_voices")
        return list(self.voices)

    # ---- remixes ----
    async def list_remixes(self, snapshot_id: UUID) -> list[dict]:
        self._maybe_fail("list_remixes")
        return [r for r in self.remixes.values() if str(r.get("snapshot_id")) == str(snapshot_id)]

    async def get_remix(self, remix_id: UUID) -> dict | None:
        self._maybe_fail("get_remix")
        return self.remixes.get(str(remix_id))

    async def snapshot_exists(self, snapshot_id: UUID) -> bool:
        self._maybe_fail("snapshot_exists")
        return str(snapshot_id) in self.snapshots

    async def insert_remix(self, row: dict) -> dict:
        self._maybe_fail("insert_remix")
        import uuid as _uuid

        new = dict(row)
        new.setdefault("id", _uuid.uuid4())
        self.remixes[str(new["id"])] = new
        return new

    async def update_remix_columns(self, remix_id: UUID, columns: dict) -> bool:
        self._maybe_fail("update_remix_columns")
        bad = set(columns) - WRITABLE_REMIX_COLUMNS
        if bad:
            raise ValueError(f"non-writable column reached adapter: {sorted(bad)}")
        row = self.remixes.get(str(remix_id))
        if row is None:
            return False
        row.update(columns)
        return True

    async def update_remix_job_column(self, remix_id: UUID, column: str, value) -> bool:
        self._maybe_fail("update_remix_job_column")
        if column not in JOB_ONLY_COLUMNS:
            raise ValueError(f"not a job-only remix column: {column!r}")
        row = self.remixes.get(str(remix_id))
        if row is None:
            return False
        row[column] = value
        return True

    async def delete_remix(self, remix_id: UUID) -> bool:
        self._maybe_fail("delete_remix")
        return self.remixes.pop(str(remix_id), None) is not None

    async def get_book_id_for_remix(self, remix_id: UUID) -> UUID | None:
        row = self.remixes.get(str(remix_id))
        if row is None:
            return None
        snap = self.snapshots.get(str(row.get("snapshot_id")))
        return snap.get("book_id") if snap else None

    # ---- jobs ----
    async def get_job(self, job_id: UUID) -> dict | None:
        self._maybe_fail("get_job")
        return self.jobs.get(str(job_id))

    async def list_stale_jobs(self, running_before, queued_before) -> list[dict]:
        self._maybe_fail("list_stale_jobs")
        # Mirror prod: only sweep rows this service authored (params.source scope).
        out: list[dict] = []
        for j in self.jobs.values():
            if (j.get("params") or {}).get("source") != "remix-swap-service":
                continue
            status = j.get("status")
            if status == "running":
                upd = j.get("updated_at")
                if upd is not None and upd < running_before:
                    out.append(j)
            elif status == "queued":
                crt = j.get("created_at")
                if crt is not None and crt < queued_before:
                    out.append(j)
        return out

    async def get_jobs(self, ids: list[UUID]) -> list[dict]:
        self._maybe_fail("get_jobs")
        wanted = {str(i) for i in ids}
        return [j for j in self.jobs.values() if str(j["id"]) in wanted]

    async def find_active_job(self, remix_id: UUID, job_type: str) -> dict | None:
        for j in self.jobs.values():
            if (
                j.get("type") == job_type
                and j.get("status") in ("queued", "running")
                and str((j.get("params") or {}).get("remix_id")) == str(remix_id)
            ):
                return j
        return None

    async def has_active_job(self, remix_id: UUID) -> bool:
        self._maybe_fail("has_active_job")
        return any(
            j.get("status") in ("queued", "running")
            and str((j.get("params") or {}).get("remix_id")) == str(remix_id)
            for j in self.jobs.values()
        )

    async def insert_job(self, row: dict) -> dict:
        import uuid as _uuid

        new = dict(row)
        new.setdefault("id", _uuid.uuid4())
        new.setdefault("status", "queued")
        self.jobs[str(new["id"])] = new
        return new

    async def update_job(self, job_id: UUID, fields: dict, expect_status: str | None = None) -> bool:
        row = self.jobs.get(str(job_id))
        if row is None:
            return False
        if expect_status is not None and row.get("status") != expect_status:
            return False
        row.update(fields)
        return True

    async def insert_ai_log(self, row: dict) -> None:
        return None

    # ---- prompt templates ----
    async def get_prompt_template(self, key: str) -> dict | None:
        self._maybe_fail("get_prompt_template")
        return self.prompt_templates.get(str(key))
