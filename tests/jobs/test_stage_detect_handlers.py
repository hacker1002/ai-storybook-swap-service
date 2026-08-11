"""Handler unit tests for the P3b STAGE + DETECT jobs (no DB, no network).

Fake adapter (module seam) + fake storage + AsyncMock'd cores. Verifies the rmbg
STAGE handler persists to the `rmbgs` column via `update_remix_job_column`, and the
detect-rmbg handler returns the `defectsBySheet` result shape. Distinct file name
(`test_stage_detect_handlers`) so it never collides with the peer's swap tests.
"""

from __future__ import annotations

import types
import uuid

import pytest

from src.db import adapter as adapter_module
from src.jobs.handlers import remix_detect_rmbg_defects as detect_rmbg_mod
from src.jobs.handlers import remix_rmbg as rmbg_mod
from src.storage import adapter as storage_adapter_module
from tests.fakes.fake_app_db_adapter import FakeAppDbAdapter
from tests.fakes.fake_app_storage_adapter import FakeAppStorageAdapter


class _Ctx:
    """Minimal JobContext stand-in (id + async report/check_cancel)."""

    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.total_steps = 1
        self.reports: list = []

    async def report(self, *, current_step, step_details):  # noqa: ANN001
        self.reports.append((current_step, dict(step_details)))

    async def check_cancel(self) -> bool:
        return False


@pytest.fixture
def wired(monkeypatch):
    db = FakeAppDbAdapter()
    adapter_module.set_adapter(db)
    storage_adapter_module.set_storage(FakeAppStorageAdapter())
    yield db
    adapter_module._ADAPTER = None
    storage_adapter_module._STORAGE = None


@pytest.mark.asyncio
async def test_rmbg_handler_persists_rmbgs_column(wired, monkeypatch):
    db = wired
    remix_id = str(uuid.uuid4())
    sheet = {
        "sheet_geometry": {"width": 10, "height": 10},
        "original_crops": [
            {"id": "c0", "spread_id": "s0", "media_url": "http://x/a.png",
             "geometry": {"x": 0, "y": 0, "w": 10, "h": 10}}
        ],
        "swap_results": [],
    }
    db.remixes[remix_id] = {"id": remix_id, "snapshot_id": str(uuid.uuid4()),
                            "rmbgs": [{"id": "b1", "crop_sheets": [sheet]}]}

    # Mock the per-sheet pipeline pieces so ONE sheet composes → rmbg → cut → persist.
    async def _compose(_req):
        return types.SimpleNamespace(png_bytes=b"PNG", skipped=[])
    monkeypatch.setattr(rmbg_mod, "compose_crop_sheet", _compose)

    import src.routers.retouch.image_remove_bg as rmbg_core_mod

    async def _remove_bg(_req, **_kw):
        return types.SimpleNamespace(image_bytes=b"RGBA")
    monkeypatch.setattr(rmbg_core_mod, "image_remove_bg_core", _remove_bg)

    async def _cut(_bytes, _geom, cut_crops, **_kw):
        return [{"spread_id": c["spread_id"], "id": c["id"], "media_url": "http://out/piece.png"}
                for c in cut_crops]
    monkeypatch.setattr(rmbg_mod, "cut_and_upload_native", _cut)

    async def _upload(*_a, **_k):
        return "http://out/sheet.png"
    monkeypatch.setattr(rmbg_mod, "upload_bytes", _upload)
    monkeypatch.setattr(rmbg_mod, "promote_is_final_for_sheet",
                        lambda *a, **k: {"promoted_count": 0, "cleared_count": 0})

    job = {
        "id": str(uuid.uuid4()),
        "params": {"remix_id": remix_id, "batch_id": "b1",
                   "model_params": {"model": "bria/remove-background"},
                   "snapshot_id": db.remixes[remix_id]["snapshot_id"],
                   "admin_ref": "adm", "sid": "sess"},
        "step_details": {"sheets": {"0": "pending"}},
        "total_steps": 1,
    }
    status, result = await rmbg_mod.handle(job, _Ctx(job["id"]))

    assert status == "completed", result
    assert result["processed_sheets"] == 1 and result["failed_sheets"] == 0
    # The rmbgs column was single-writer persisted through the job-only seam.
    persisted = db.remixes[remix_id]["rmbgs"][0]["crop_sheets"][0]["swap_results"]
    assert persisted and persisted[0]["is_selected"] is True
    assert persisted[0]["crops"][0]["media_url"] == "http://out/piece.png"


@pytest.mark.asyncio
async def test_detect_rmbg_handler_result_shape(wired, monkeypatch):
    db = wired
    remix_id = str(uuid.uuid4())
    sheet = {
        "sheet_geometry": {"width": 20, "height": 12},
        "original_crops": [
            {"id": "c0", "spread_id": "s0", "media_url": "http://x/a.png",
             "geometry": {"x": 0, "y": 0, "w": 20, "h": 12}}
        ],
        "swap_results": [{"is_selected": True, "media_url": "http://x/sel.png", "crops": []}],
    }
    db.remixes[remix_id] = {"id": remix_id, "rmbgs": [{"id": "b1", "crop_sheets": [sheet]}]}

    # Avoid constructing the real request model; the core is mocked anyway.
    monkeypatch.setattr(detect_rmbg_mod, "resolve_detect_rmbg_body", lambda *a, **k: object())

    class _Defect:
        def model_dump(self, exclude_none=True):  # noqa: ANN001
            return {"center": {"x": 1, "y": 2}, "radius": 3}

    async def _run(_body, **_kw):
        return types.SimpleNamespace(
            defects=[_Defect()],
            meta=types.SimpleNamespace(
                swappedDimensions=types.SimpleNamespace(width=20, height=12),
                defectCount=1, truncated=False,
            ),
        )
    monkeypatch.setattr(detect_rmbg_mod, "run_detect_rmbg_defects", _run)

    job = {
        "id": str(uuid.uuid4()),
        "params": {"remix_id": remix_id, "batch_id": "b1", "controls": {},
                   "admin_ref": "adm", "sid": "sess"},
        "step_details": {},
        "total_steps": 1,
    }
    status, result = await detect_rmbg_mod.handle(job, _Ctx(job["id"]))

    assert status == "completed", result
    assert result["skipped_sheets"] == 0 and result["errors"] == []
    by_sheet = result["defectsBySheet"]
    assert len(by_sheet) == 1
    entry = by_sheet[0]
    assert entry["sheet_index"] == 0
    assert entry["defectCount"] == 1 and entry["truncated"] is False
    assert entry["swappedDimensions"] == {"width": 20, "height": 12}
    assert entry["defects"][0]["radius"] == 3
