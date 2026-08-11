"""Handler-level test for the audio-swap job handler (P3b Phase 06).

Separate module (imports the ElevenLabs/narration stack) — kept apart from the
sprite/mix handler test so a missing audio dep never masks those. Exercises the
remix-not-found early return + registration through the `FakeAppDbAdapter` seam
(no ElevenLabs, no Storage). Distinct filename vs the peer agent's tests.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.core.job_types import JOB_TYPE_AUDIO_SWAP
from src.db import adapter as adapter_module
from src.jobs.runner import JobContext, _REGISTRY
import src.jobs.handlers.remix_audio_swap as audio_mod
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter


@pytest.fixture
def fake():
    a = FakeAppDbAdapter()
    adapter_module.set_adapter(a)
    yield a
    adapter_module._ADAPTER = None


def _run(coro):
    return asyncio.run(coro)


def test_audio_handler_registered():
    assert JOB_TYPE_AUDIO_SWAP in _REGISTRY


def test_audio_handler_remix_not_found_fails(fake):
    job = {
        "id": str(uuid.uuid4()),
        "book_id": None,
        "step_details": {},
        "params": {"remix_id": str(uuid.uuid4())},
    }
    ctx = JobContext(id=str(uuid.uuid4()), total_steps=1)
    status, result = _run(audio_mod.handle(job, ctx))
    assert status == "failed"
    assert result["errors"][0]["message"] == "remix_not_found"
    assert result["updated_spreads"] == 0
