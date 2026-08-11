"""Handler-level test for `remix_sprite_swap` (P3b Phase 06).

Drives `handle(job, ctx)` DIRECTLY against the in-memory `FakeAppDbAdapter` +
`FakeAppStorageAdapter`, with the AI core (`run_swap_sprite_sheet`), the cut
helper, the object-pool resolver and `upload_bytes` all monkeypatched (AsyncMock /
stub) so no Gemini / Storage / cut I/O runs. Asserts the happy path returns
`("completed", result)` with `swapped_sheets == 1` AND that the swap is persisted
to the `sprites` column via `update_remix_columns`.

Named distinctly (`test_remix_sprite_swap_handler`) to avoid colliding with the
enqueue-route test filenames or the peer agent's handler tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.jobs.handlers.remix_sprite_swap as h
from src.db import adapter as adapter_module
from src.jobs.runner import JobContext
from src.storage import adapter as storage_module
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter
from tests.fakes.fake_app_storage_adapter import FakeAppStorageAdapter


@pytest.fixture
def fake():
    a = FakeAppDbAdapter()
    adapter_module.set_adapter(a)
    storage_module.set_storage(FakeAppStorageAdapter())
    yield a
    adapter_module._ADAPTER = None
    storage_module._STORAGE = None


def _seed(fake: FakeAppDbAdapter) -> tuple[str, str, str, str]:
    book_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    remix_id = str(uuid.uuid4())
    sprite_id = str(uuid.uuid4())
    fake.snapshots[snapshot_id] = {"id": snapshot_id, "book_id": book_id, "characters": []}
    fake.remixes[remix_id] = {
        "id": remix_id,
        "snapshot_id": snapshot_id,
        "remix_config": {"characters": []},
        "sprites": [
            {
                "id": sprite_id,
                "crop_sheets": [
                    {
                        "sheet_geometry": {"width": 100, "height": 100},
                        "original_crops": [{"object_key": "leela"}],
                        "swap_results": [],
                    }
                ],
            }
        ],
    }
    return remix_id, sprite_id, snapshot_id, book_id


def _patch_ai(monkeypatch):
    pool = SimpleNamespace(
        lineup=["leela"], missing=[], object_count=1, object_map={"leela": {}}
    )
    monkeypatch.setattr(h, "resolve_sprite_object_map", lambda *a, **k: pool)
    monkeypatch.setattr(h, "select_sheet_objects", lambda *a, **k: [{"object_key": "leela"}])
    monkeypatch.setattr(
        h,
        "_eligible_sheet_crops",
        lambda sheet: [
            {
                "type": "character",
                "object_key": "leela",
                "variant_key": "base",
                "geometry": {"x": 0, "y": 0, "w": 50, "h": 50},
            }
        ],
    )
    # Bypass the real Pydantic core-request build (its crops/swap_objects require
    # full media_url/human fields we deliberately stub past — the mocked core
    # ignores the request object anyway).
    monkeypatch.setattr(h, "_build_core_request", lambda *a, **k: object())
    monkeypatch.setattr(
        h,
        "run_swap_sprite_sheet",
        AsyncMock(return_value=SimpleNamespace(image_bytes=b"PNGDATA", composed_sheet_url=None)),
    )
    monkeypatch.setattr(h, "upload_bytes", AsyncMock(return_value="https://storage.test/sheet.png"))
    monkeypatch.setattr(
        h,
        "cut_sprite_sheet_to_crops",
        AsyncMock(
            return_value=[
                {
                    "type": "character",
                    "object_key": "leela",
                    "variant_key": "base",
                    "geometry": {"x": 0, "y": 0, "w": 50, "h": 50},
                    "media_url": "https://storage.test/crop.png",
                }
            ]
        ),
    )


async def test_sprite_handler_completes_and_persists(fake, monkeypatch):
    remix_id, sprite_id, _snap, book_id = _seed(fake)
    _patch_ai(monkeypatch)

    job_id = uuid.uuid4()
    fake.jobs[str(job_id)] = {"id": job_id, "status": "running", "cancel_requested": False}
    job = {
        "id": job_id,
        "book_id": book_id,
        "params": {
            "remix_id": remix_id,
            "sprite_id": sprite_id,
            "force_resweep": False,
            "model_params": {"model": "google/nano-banana-pro", "params": {}},
            "admin_ref": "admin-1",
            "sid": "sid-1",
        },
        "step_details": {"sheets": {"0": "pending"}},
        "total_steps": 1,
    }
    ctx = JobContext(id=job_id, total_steps=1)

    status, result = await h.handle(job, ctx)

    assert status == "completed"
    assert result["swapped_sheets"] == 1
    assert result["failed_sheets"] == 0
    assert result["errors"] == []
    # Persisted to the disjoint `sprites` column with the appended swap_result.
    persisted = fake.remixes[remix_id]["sprites"][0]["crop_sheets"][0]["swap_results"]
    assert len(persisted) == 1 and persisted[0]["is_selected"] is True
    # AI core received the threaded remix_id attribution.
    ai_kwargs = h.run_swap_sprite_sheet.await_args.kwargs
    assert ai_kwargs["ai_context"].remix_id == remix_id
    assert ai_kwargs["ai_context"].admin_ref == "admin-1"
