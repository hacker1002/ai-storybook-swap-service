"""Pytest fixtures — deterministic, no DB, no network.

Required env is set BEFORE any `src` import so `Settings()` (constructed at import
time) never touches `.env`/a real DSN. The TestClient is built WITHOUT the context
manager so the lifespan never runs → the real asyncpg pool is never opened; the DB
seam is the injected `FakeAppDbAdapter`.
"""

from __future__ import annotations

import os

# Must precede `src` imports — settings validates these at construction.
os.environ.setdefault("APP_DB_URL", "postgresql://unit-test-never-connected/db")
os.environ["REMIX_EDITOR_TOKEN_SECRET"] = "test-secret-constant-do-not-reuse"
# ADR-053: handoff secret is REQUIRED at construction; internal key drives the S2S guard.
os.environ["REMIX_EDITOR_HANDOFF_SECRET"] = "test-handoff-secret-do-not-reuse"
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

# Force LangSmith tracing OFF for the whole suite. Unit tests call the
# @traceable/langchain-wrapped AI seams with MOCKS; if the developer's shell has
# LANGCHAIN_TRACING_V2=true, those mocked runs would ship to LangSmith and pollute
# the real project. Only genuine API calls (live server) should trace. Hard-set
# (not setdefault) so a truthy shell value can't leak into a test run.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from scripts.mint_dev_editor_token import mint_handoff_assertion, mint_token  # noqa: E402
from src.auth.session_stores import reset_stores_for_test  # noqa: E402
from src.db import adapter as adapter_module  # noqa: E402
from src.main import app  # noqa: E402
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter  # noqa: E402

_TEST_SECRET = "test-secret-constant-do-not-reuse"
_TEST_HANDOFF_SECRET = "test-handoff-secret-do-not-reuse"
_INTERNAL_HEADERS = {"X-API-Key": "test-internal-key"}


@pytest.fixture(autouse=True)
def _reset_session_stores():
    """Clear in-memory jti/denylist state around every test (module-level state leaks)."""
    reset_stores_for_test()
    yield
    reset_stores_for_test()


@pytest.fixture
def internal_headers() -> dict:
    return dict(_INTERNAL_HEADERS)


@pytest.fixture
def handoff_assertion() -> str:
    return mint_handoff_assertion(secret=_TEST_HANDOFF_SECRET)


@pytest.fixture
def fake_adapter():
    fake = FakeAppDbAdapter()
    adapter_module.set_adapter(fake)
    yield fake
    adapter_module._ADAPTER = None  # teardown — no leak between tests


@pytest.fixture
def client():
    # No `with` -> lifespan skipped -> real pool never opened.
    return TestClient(app)


@pytest.fixture
def editor_token() -> str:
    return mint_token(secret=_TEST_SECRET)


@pytest.fixture
def auth_headers(editor_token) -> dict:
    return {"Authorization": f"Bearer {editor_token}"}


@pytest.fixture
def expired_token() -> str:
    return mint_token(secret=_TEST_SECRET, expired=True)


@pytest.fixture
def viewer_token() -> str:
    return mint_token(secret=_TEST_SECRET, role="viewer")
