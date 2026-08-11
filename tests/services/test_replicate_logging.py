"""`replicate_client` re-coupled ADR-050 choke-point logging (Phase 05 reconcile).

Proves one fire-and-forget `log_ai_request` row per Replicate call (success AND the
post-create error path), `ai_request_id` populated, output URL recorded as an
`output_blobs` entry (URL metadata, no re-host), and `usage_unit='seconds'` — all
without a real Replicate round-trip (the async prediction is faked).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services import replicate_client as rc
from src.services.ai_usage import AiCallContext


class _FakePrediction:
    def __init__(self, *, output, status="succeeded", error=None):
        self.output = output
        self.status = status
        self.error = error
        self.id = "pred-123"
        self.metrics = {"predict_time": 1.5}
        self.logs = ""

    async def async_wait(self):
        return None


def _patch_client(monkeypatch, prediction):
    async def _create(*a, **k):
        return prediction

    fake = SimpleNamespace(predictions=SimpleNamespace(async_create=_create))
    monkeypatch.setattr(rc, "get_replicate_client", lambda: fake)
    # neutralize the retry wrapper's own semantics — just call the factory.
    async def _retry(factory, label=""):
        return await factory()

    monkeypatch.setattr(rc, "create_with_429_retry", _retry)


@pytest.mark.asyncio
async def test_remove_bg_logs_success(monkeypatch):
    captured = []
    monkeypatch.setattr(rc, "log_ai_request", lambda e: captured.append(e))
    _patch_client(monkeypatch, _FakePrediction(output="https://cdn.test/out.png"))

    result = await rc.run_remove_bg(
        {"image": "https://in.test/x.png"},
        ai_context=AiCallContext(remix_id="22222222-2222-2222-2222-222222222222"),
    )
    assert result.output == "https://cdn.test/out.png"
    assert result.ai_request_id  # correlation id populated
    assert result.output_files == ()  # no re-host in this service
    assert len(captured) == 1
    entry = captured[0]
    assert entry.provider == "replicate"
    assert entry.status == "success"
    assert entry.operation == "retouch.image_remove_bg.replicate"
    assert entry.usage_unit == "seconds"
    # raw output URL recorded as an output blob (URL string → logger makes {url}).
    assert "https://cdn.test/out.png" in entry.output_blobs


@pytest.mark.asyncio
async def test_remove_bg_logs_error_on_post_create_failure(monkeypatch):
    captured = []
    monkeypatch.setattr(rc, "log_ai_request", lambda e: captured.append(e))
    # prediction created but failed → post-create error path logs an error row.
    _patch_client(
        monkeypatch,
        _FakePrediction(output=None, status="failed", error="boom upstream"),
    )

    with pytest.raises(HTTPException):
        await rc.run_remove_bg({"image": "https://in.test/x.png"})

    assert len(captured) == 1
    assert captured[0].status == "error"
    assert captured[0].provider == "replicate"


@pytest.mark.asyncio
async def test_remove_bg_custom_operation_tag(monkeypatch):
    captured = []
    monkeypatch.setattr(rc, "log_ai_request", lambda e: captured.append(e))
    _patch_client(monkeypatch, _FakePrediction(output="https://cdn.test/out.png"))

    await rc.run_remove_bg(
        {"image": "https://in.test/x.png"}, operation="actor.rmbg"
    )
    assert captured[0].operation == "actor.rmbg"
