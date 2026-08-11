"""Handler-level tests for the sprite + mix swap job handlers (P3b Phase 06).

Exercises the real handler through the `FakeAppDbAdapter` seam (no AI, no
storage): the remix-not-found early return + registration. The full swap path is
covered by the live test-scripts (real Gemini) — mocking the whole crop pipeline
here would test the mocks, not the port. Distinct filename to avoid colliding with
the peer agent's handler tests.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.core.job_types import JOB_TYPE_MIX_SWAP, JOB_TYPE_SPRITE_SWAP
from src.db import adapter as adapter_module
from src.jobs.runner import JobContext, _REGISTRY
import src.jobs.handlers.remix_mix_swap as mix_mod
import src.jobs.handlers.remix_sprite_swap as sprite_mod
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter


@pytest.fixture
def fake():
    a = FakeAppDbAdapter()
    adapter_module.set_adapter(a)
    yield a
    adapter_module._ADAPTER = None


def _run(coro):
    return asyncio.run(coro)


def _ctx() -> JobContext:
    return JobContext(id=str(uuid.uuid4()), total_steps=1)


def test_handlers_registered_with_canonical_types():
    # A mistyped `type` string is NOT rejected by the DB — the FE silently drops
    # the job. Assert the registry keys match the canonical constants.
    assert JOB_TYPE_SPRITE_SWAP in _REGISTRY
    assert JOB_TYPE_MIX_SWAP in _REGISTRY


def test_sprite_handler_remix_not_found_fails(fake):
    job = {
        "id": str(uuid.uuid4()),
        "book_id": None,
        "step_details": {},
        "params": {
            "remix_id": str(uuid.uuid4()),
            "sprite_id": str(uuid.uuid4()),
            "model_params": {"model": "google/nano-banana-pro", "params": {}},
        },
    }
    status, result = _run(sprite_mod.handle(job, _ctx()))
    assert status == "failed"
    assert result["errors"][0]["message"] == "remix_not_found"
    assert result["swapped_sheets"] == 0


def test_mix_handler_remix_not_found_fails(fake):
    job = {
        "id": str(uuid.uuid4()),
        "book_id": None,
        "step_details": {},
        "params": {
            "remix_id": str(uuid.uuid4()),
            "batch_id": str(uuid.uuid4()),
            "model_params": {"model": "google/nano-banana-pro", "params": {}},
        },
    }
    status, result = _run(mix_mod.handle(job, _ctx()))
    assert status == "failed"
    assert result["errors"][0]["message"] == "remix_not_found"
    assert result["swapped_sheets"] == 0
