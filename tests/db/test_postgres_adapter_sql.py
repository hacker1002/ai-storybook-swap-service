"""Adapter pure-logic tests (no DB): allowlist guard + rowcount parsing +
Protocol conformance for both the fake and the real adapter."""

from __future__ import annotations

import uuid

import pytest

from src.db.adapter import AppDbAdapter
from src.db.postgres_adapter import PostgresAppDbAdapter, _rowcount
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter


def test_rowcount_parses_command_tags():
    assert _rowcount("UPDATE 1") == 1
    assert _rowcount("DELETE 0") == 0
    assert _rowcount("INSERT 0 3") == 3
    assert _rowcount("garbage") == 0


async def test_update_remix_columns_rejects_non_writable_before_db():
    # pool=None proves the allowlist guard raises BEFORE any acquire().
    adapter = PostgresAppDbAdapter(pool=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await adapter.update_remix_columns(uuid.uuid4(), {"remix_config": {}})


async def test_update_remix_columns_rejects_empty():
    adapter = PostgresAppDbAdapter(pool=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await adapter.update_remix_columns(uuid.uuid4(), {})


def test_fake_implements_protocol():
    assert isinstance(FakeAppDbAdapter(), AppDbAdapter)


def test_postgres_implements_protocol():
    assert isinstance(PostgresAppDbAdapter(pool=None), AppDbAdapter)  # type: ignore[arg-type]
