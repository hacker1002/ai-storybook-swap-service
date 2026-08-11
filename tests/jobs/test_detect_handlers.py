"""Unit tests for the DETECT job handlers (jobs 11/12/13) — orchestration only.

The AI cores (`run_detect_*`) + the per-sheet body resolver are mocked (AsyncMock /
monkeypatch) so these assert the handler's scope build, `result.defectsBySheet`
shape, per-sheet error recording, and cooperative-cancel — WITHOUT any real Gemini
call. Adapter seam = `fake_adapter` (conftest). Registry reset is LOCAL (autouse
below) so importing the handler modules here can't collide with the central
`handlers/__init__.py` side-effect import.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.jobs import runner
from src.jobs.handlers import remix_detect_rmbg_defects as h_rmbg
from src.jobs.runner import JobContext

REMIX_ID = "11111111-1111-1111-1111-111111111111"
BATCH_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _preserve_registry():
    """Save/restore the global handler registry so this module's imports never
    leak a registration into a sibling test module (do NOT edit conftest)."""
    reg = dict(runner._REGISTRY)
    fin = dict(runner._FINALIZE_HOOKS)
    yield
    runner._REGISTRY.clear()
    runner._REGISTRY.update(reg)
    runner._FINALIZE_HOOKS.clear()
    runner._FINALIZE_HOOKS.update(fin)


def _fake_defect(cat: str = "edge_halo"):
    return SimpleNamespace(model_dump=lambda exclude_none=True: {"category": cat, "radius": 5})


def _fake_result(defect_count: int = 1):
    meta = SimpleNamespace(
        swappedDimensions=SimpleNamespace(width=1024, height=768),
        defectCount=defect_count,
        truncated=False,
    )
    return SimpleNamespace(defects=[_fake_defect() for _ in range(defect_count)], meta=meta)


def _sheet(selected: bool) -> dict:
    swap_results = []
    if selected:
        swap_results = [{"is_selected": True, "media_url": "http://x/rmbg.png", "crops": []}]
    return {"sheet_geometry": {"width": 1024, "height": 768}, "swap_results": swap_results}


def _seed_rmbg_remix(fake, sheets: list[dict]):
    fake.remixes[REMIX_ID] = {
        "id": REMIX_ID,
        "snapshot_id": "33333333-3333-3333-3333-333333333333",
        "rmbgs": [{"id": BATCH_ID, "crop_sheets": sheets}],
    }


def _ctx_and_job(fake, *, cancel: bool = False):
    job_id = uuid.uuid4()
    fake.jobs[str(job_id)] = {"id": job_id, "status": "running", "cancel_requested": cancel}
    job = {
        "id": job_id,
        "user_id": "svc-user",
        "book_id": "44444444-4444-4444-4444-444444444444",
        "params": {"remix_id": REMIX_ID, "batch_id": BATCH_ID, "controls": {}},
    }
    ctx = JobContext(id=job_id, total_steps=2)
    return ctx, job


async def test_detect_rmbg_happy_shape(fake_adapter, monkeypatch):
    # 2 eligible sheets + 1 non-selected (skipped).
    _seed_rmbg_remix(fake_adapter, [_sheet(True), _sheet(True), _sheet(False)])
    monkeypatch.setattr(h_rmbg, "resolve_detect_rmbg_body", lambda sheet, sel, controls: SimpleNamespace())
    core = AsyncMock(return_value=_fake_result(2))
    monkeypatch.setattr(h_rmbg, "run_detect_rmbg_defects", core)

    ctx, job = _ctx_and_job(fake_adapter)
    status, result = await h_rmbg.handle(job, ctx)

    assert status == "completed"
    assert len(result["defectsBySheet"]) == 2
    assert result["skipped_sheets"] == 1
    assert result["errors"] == []
    first = result["defectsBySheet"][0]
    assert first["defectCount"] == 2
    assert first["swappedDimensions"] == {"width": 1024, "height": 768}
    assert core.await_count == 2


async def test_detect_rmbg_per_sheet_error_non_fatal(fake_adapter, monkeypatch):
    _seed_rmbg_remix(fake_adapter, [_sheet(True), _sheet(True)])
    monkeypatch.setattr(h_rmbg, "resolve_detect_rmbg_body", lambda sheet, sel, controls: SimpleNamespace())

    from src.services.remix.errors import RemixDomainError

    calls = {"n": 0}

    async def _core(body, ai_context=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RemixDomainError(status=502, code="LLM_ERROR", message="boom")
        return _fake_result(1)

    monkeypatch.setattr(h_rmbg, "run_detect_rmbg_defects", _core)

    ctx, job = _ctx_and_job(fake_adapter)
    status, result = await h_rmbg.handle(job, ctx)

    # One sheet failed (recorded), the other inspected — job still completes.
    assert status == "completed"
    assert len(result["defectsBySheet"]) == 1
    assert any(e["code"] == "LLM_ERROR" for e in result["errors"])


async def test_detect_rmbg_cancel_pre_gather(fake_adapter, monkeypatch):
    _seed_rmbg_remix(fake_adapter, [_sheet(True)])
    monkeypatch.setattr(h_rmbg, "resolve_detect_rmbg_body", lambda sheet, sel, controls: SimpleNamespace())
    core = AsyncMock(return_value=_fake_result(1))
    monkeypatch.setattr(h_rmbg, "run_detect_rmbg_defects", core)

    ctx, job = _ctx_and_job(fake_adapter, cancel=True)
    status, result = await h_rmbg.handle(job, ctx)

    assert status == "cancelled"
    assert result["defectsBySheet"] == []
    core.assert_not_awaited()


async def test_detect_rmbg_unknown_remix_failed(fake_adapter, monkeypatch):
    # No remix seeded → REMIX_NOT_FOUND recorded, job failed.
    monkeypatch.setattr(h_rmbg, "run_detect_rmbg_defects", AsyncMock())
    ctx, job = _ctx_and_job(fake_adapter)
    status, result = await h_rmbg.handle(job, ctx)
    assert status == "failed"
    assert result["errors"][0]["code"] == "REMIX_NOT_FOUND"
