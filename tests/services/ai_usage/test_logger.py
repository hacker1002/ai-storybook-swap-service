"""logger.py — the async fire-and-forget insert path.

Asserts the row shape the phase mandates (user_id NULL, nested request.audit.source,
client-minted `id` — image-api parity restored 260812 — allowlist-clean), the
remix→book bridge + ctx caching, drain, and the two never-raise invariants
(adapter failure / no running loop)."""

from __future__ import annotations

import uuid

import pytest

from src.db import adapter as adapter_module
from src.db.postgres_adapter import _AI_LOG_COLUMNS
from src.services.ai_usage import logger as log_mod
from src.services.ai_usage.context import AiCallContext
from src.services.ai_usage.logger import AiLogEntry, drain, log_ai_request
from src.services.ai_usage.pricing import compute_cost
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter


class _CapturingAdapter(FakeAppDbAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.ai_rows: list[dict] = []
        self.bridge_calls = 0

    async def insert_ai_log(self, row: dict) -> None:
        self._maybe_fail("insert_ai_log")
        # Mirror the REAL adapter's allowlist guard so a stray column fails the test
        # (the fake's base insert_ai_log is a no-op that would hide it).
        bad = set(row) - _AI_LOG_COLUMNS
        if bad:
            raise ValueError(f"non-allowlisted: {sorted(bad)}")
        self.ai_rows.append(row)

    async def get_book_id_for_remix(self, remix_id):
        self.bridge_calls += 1
        return await super().get_book_id_for_remix(remix_id)


@pytest.fixture
def cap_adapter():
    a = _CapturingAdapter()
    adapter_module.set_adapter(a)
    yield a
    adapter_module._ADAPTER = None
    log_mod._LOG_TASKS.clear()


def _entry(ctx, **kw):
    base = dict(
        provider="gemini", operation="remix.swap-mix", model="gemini-3-pro-image",
        status="success", context=ctx, request={"prompt": "hi"},
        cost=compute_cost("gemini", "gemini-3-pro-image", {"input_tokens": 10, "output_tokens": 20}),
    )
    base.update(kw)
    return AiLogEntry(**base)


async def test_insert_writes_expected_row(cap_adapter):
    ctx = AiCallContext(remix_id=str(uuid.uuid4()), admin_ref="admin-42", sid="sess-9")
    log_ai_request(_entry(ctx))
    await drain()

    assert len(cap_adapter.ai_rows) == 1
    row = cap_adapter.ai_rows[0]
    assert row["user_id"] is None
    assert row["request"]["audit"] == {"admin_ref": "admin-42", "sid": "sess-9", "source": "remix-swap-service"}
    # No entry.id passed → logger mints a fallback uuid4 (insert never fails on it).
    assert isinstance(row["id"], uuid.UUID)
    assert isinstance(row["remix_id"], uuid.UUID)
    assert row["cost_usd"] is not None and row["cost_source"] == "token_table"


async def test_client_minted_id_becomes_row_id(cap_adapter):
    # The pre-call new_request_id() a choke point surfaces as ai_request_id in its
    # envelope must be the row id — envelope id resolvable via get_ai_log.
    rid = str(uuid.uuid4())
    log_ai_request(_entry(AiCallContext(), id=rid))
    await drain()
    assert cap_adapter.ai_rows[0]["id"] == uuid.UUID(rid)


async def test_malformed_id_falls_back_to_minted_uuid(cap_adapter):
    log_ai_request(_entry(AiCallContext(), id="not-a-uuid"))
    await drain()
    assert isinstance(cap_adapter.ai_rows[0]["id"], uuid.UUID)


async def test_book_id_resolved_from_remix_bridge_and_cached(cap_adapter):
    book_id = uuid.uuid4()
    snap_id = uuid.uuid4()
    remix_id = uuid.uuid4()
    cap_adapter.seed("snapshots", [{"id": snap_id, "book_id": book_id}])
    cap_adapter.seed("remixes", [{"id": remix_id, "snapshot_id": snap_id}])

    ctx = AiCallContext(remix_id=str(remix_id))  # no explicit book_id
    # Two AI calls share ONE ctx → bridge must be queried at most once (cache).
    log_ai_request(_entry(ctx))
    log_ai_request(_entry(ctx))
    await drain()

    assert len(cap_adapter.ai_rows) == 2
    assert all(r["book_id"] == book_id for r in cap_adapter.ai_rows)
    assert cap_adapter.bridge_calls == 1  # cached on the ctx


async def test_explicit_book_id_skips_bridge(cap_adapter):
    bid = uuid.uuid4()
    ctx = AiCallContext(remix_id=str(uuid.uuid4()), book_id=str(bid))
    log_ai_request(_entry(ctx))
    await drain()
    assert cap_adapter.bridge_calls == 0
    assert cap_adapter.ai_rows[0]["book_id"] == bid


async def test_insert_failure_never_raises(cap_adapter):
    cap_adapter.fail_on("insert_ai_log", RuntimeError("db down"))
    ctx = AiCallContext(remix_id=str(uuid.uuid4()))
    log_ai_request(_entry(ctx))  # must not raise
    await drain()  # must not raise despite the failed insert
    assert cap_adapter.ai_rows == []


def test_log_request_off_event_loop_is_a_noop(cap_adapter):
    # No running loop → warn + drop, never raises (sync context).
    log_ai_request(_entry(AiCallContext(remix_id=str(uuid.uuid4()))))
    assert cap_adapter.ai_rows == []
