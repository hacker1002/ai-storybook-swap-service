"""AiCallContext — the documented `str`-coercion contract (a raw UUID id must NOT
survive as a UUID, else image-api silently dropped the log row)."""

from __future__ import annotations

import uuid

from src.services.ai_usage.context import AiCallContext


def test_uuid_id_fields_coerced_to_str():
    rid = uuid.uuid4()
    bid = uuid.uuid4()
    ctx = AiCallContext(remix_id=rid, book_id=bid, admin_ref=uuid.uuid4(), sid=uuid.uuid4())
    assert isinstance(ctx.remix_id, str) and ctx.remix_id == str(rid)
    assert isinstance(ctx.book_id, str) and ctx.book_id == str(bid)
    assert isinstance(ctx.admin_ref, str)
    assert isinstance(ctx.sid, str)


def test_str_ids_pass_through_unchanged():
    s = str(uuid.uuid4())
    ctx = AiCallContext(remix_id=s, job_id="job-123")
    assert ctx.remix_id == s and ctx.job_id == "job-123"


def test_none_stays_none():
    ctx = AiCallContext()
    assert ctx.book_id is None and ctx.remix_id is None and ctx.user_id is None
    assert ctx.admin_ref is None and ctx.sid is None


def test_book_cache_is_mutable_on_frozen_ctx():
    ctx = AiCallContext(remix_id=str(uuid.uuid4()))
    # frozen blocks field reassignment, NOT mutation of the cache dict.
    ctx._book_cache["book_id"] = "cached"
    assert ctx._book_cache["book_id"] == "cached"
