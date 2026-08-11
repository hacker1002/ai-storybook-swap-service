"""Handler-level test for `remix_audio_swap` (P3b Phase 06).

Drives `handle(job, ctx)` DIRECTLY against the in-memory `FakeAppDbAdapter`, with
the ElevenLabs TTS core (`run_narrate_script`) monkeypatched (AsyncMock) so no
ElevenLabs / Storage I/O runs. One single-chunk textbox → the combine shortcut
path (no loopback). Asserts `("completed", result)` with `updated_spreads == 1`
AND that the regenerated audio is persisted to the `illustration` column.

Named distinctly to avoid colliding with the enqueue-route tests or the peer
agent's handler tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.jobs.handlers.remix_audio_swap as h
from src.db import adapter as adapter_module
from src.jobs.runner import JobContext
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter


@pytest.fixture
def fake():
    a = FakeAppDbAdapter()
    adapter_module.set_adapter(a)
    yield a
    adapter_module._ADAPTER = None


def _seed(fake: FakeAppDbAdapter) -> tuple[str, str]:
    book_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    remix_id = str(uuid.uuid4())
    fake.snapshots[snapshot_id] = {"id": snapshot_id, "book_id": book_id}
    fake.voices.append({"id": "v1", "eleven_id": "el1"})
    fake.remixes[remix_id] = {
        "id": remix_id,
        "snapshot_id": snapshot_id,
        "remix_config": {
            "languages": [{"code": "en_US", "is_enabled": True}],
            "voices": [{"key": "narrator", "voice_id": "v1", "is_enabled": True}],
        },
        "illustration": {
            "spreads": [
                {
                    "id": "sp1",
                    "textboxes": [
                        {
                            "id": "tb1",
                            "en_US": {
                                "audio": {
                                    "chunks": [
                                        {
                                            "script": "hello",
                                            "script_synced": False,
                                            "voice_id": "v1",
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        },
    }
    return remix_id, book_id


async def test_audio_handler_completes_and_persists(fake, monkeypatch):
    remix_id, book_id = _seed(fake)
    monkeypatch.setattr(
        h,
        "run_narrate_script",
        AsyncMock(
            return_value=SimpleNamespace(
                audio_url="https://storage.test/tts.mp3", words=[], raw_alignment={}
            )
        ),
    )

    job_id = uuid.uuid4()
    fake.jobs[str(job_id)] = {"id": job_id, "status": "running", "cancel_requested": False}
    job = {
        "id": job_id,
        "book_id": book_id,
        "params": {
            "remix_id": remix_id,
            "max_concurrent_chunks_per_textbox": 4,
            "admin_ref": "admin-1",
            "sid": "sid-1",
        },
        "step_details": {"spreads": {"sp1": "pending"}},
        "total_steps": 1,
    }
    ctx = JobContext(id=job_id, total_steps=1)

    status, result = await h.handle(job, ctx)

    assert status == "completed"
    assert result["updated_spreads"] == 1
    assert result["failed_spreads"] == 0
    assert result["total_chunks_regenerated"] == 1
    assert result["errors"] == []
    # Regenerated audio persisted to the `illustration` column (single-chunk shortcut).
    audio = fake.remixes[remix_id]["illustration"]["spreads"][0]["textboxes"][0]["en_US"]["audio"]
    assert audio["combined_audio_url"] == "https://storage.test/tts.mp3"
    # ElevenLabs core received the threaded remix_id + audit attribution.
    ai_kwargs = h.run_narrate_script.await_args.kwargs
    assert ai_kwargs["ai_context"].remix_id == remix_id
    assert ai_kwargs["ai_context"].sid == "sid-1"
