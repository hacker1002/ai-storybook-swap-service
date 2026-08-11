"""`gemini_ainvoke` re-coupled ADR-050 choke-point logging (Phase 05 reconcile).

Phase 02 ported invoke.py WITHOUT logging; Phase 05 re-couples it to
`src.services.ai_usage`. These tests prove one fire-and-forget `log_ai_request` row
per call (success AND error), that `ai_request_id` is populated, and that a provider
exception propagates RAW after the error row — without any real Gemini/DB I/O.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.ai_usage import AiCallContext
from src.services.gemini import invoke as invoke_mod


class _FakeLLM:
    def __init__(self, *, message=None, exc=None):
        self._message = message
        self._exc = exc

    def __call__(self, **kwargs):  # ChatGoogleGenerativeAI(**llm_kwargs)
        return self

    async def ainvoke(self, messages, config=None):
        if self._exc is not None:
            raise self._exc
        return self._message


def _fake_message():
    # AIMessage-like: text content + usage_metadata.
    return SimpleNamespace(
        content="hello",
        usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    )


@pytest.mark.asyncio
async def test_gemini_ainvoke_logs_success(monkeypatch):
    captured = []
    monkeypatch.setattr(invoke_mod, "ChatGoogleGenerativeAI", _FakeLLM(message=_fake_message()))
    monkeypatch.setattr(invoke_mod, "log_ai_request", lambda entry: captured.append(entry))

    result = await invoke_mod.gemini_ainvoke(
        model="gemini-3-pro-image-preview",
        messages=[SimpleNamespace(content="hi")],
        run_name="remix.swap_sprite.test",
        ai_context=AiCallContext(remix_id="11111111-1111-1111-1111-111111111111"),
    )

    assert result.ai_request_id  # populated (non-empty correlation id)
    assert result.output_files == ()  # no content-addressed re-hosting in this service
    assert len(captured) == 1
    entry = captured[0]
    assert entry.provider == "gemini"
    assert entry.status == "success"
    assert entry.operation == "remix.swap_sprite.test"
    assert entry.total_tokens == 13
    assert entry.context.remix_id == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_gemini_ainvoke_logs_error_and_reraises(monkeypatch):
    captured = []
    boom = RuntimeError("provider exploded")
    monkeypatch.setattr(invoke_mod, "ChatGoogleGenerativeAI", _FakeLLM(exc=boom))
    monkeypatch.setattr(invoke_mod, "log_ai_request", lambda entry: captured.append(entry))

    with pytest.raises(RuntimeError, match="provider exploded"):
        await invoke_mod.gemini_ainvoke(
            model="gemini-3-pro-image-preview",
            messages=[SimpleNamespace(content="hi")],
            run_name="remix.swap.err",
        )

    assert len(captured) == 1
    assert captured[0].status == "error"
    assert "provider exploded" in (captured[0].error or "")


@pytest.mark.asyncio
async def test_gemini_ainvoke_real_logger_swallows(monkeypatch):
    """With the REAL `log_ai_request` (no adapter set), the fire-and-forget insert
    is scheduled and swallows its own error — the success path returns cleanly."""
    monkeypatch.setattr(invoke_mod, "ChatGoogleGenerativeAI", _FakeLLM(message=_fake_message()))
    # do NOT patch log_ai_request → real fire-and-forget path runs; adapter unset →
    # the background insert swallows internally, never surfacing to this caller.
    result = await invoke_mod.gemini_ainvoke(
        model="m",
        messages=[SimpleNamespace(content="hi")],
        run_name="x",
        ai_context=AiCallContext(),
    )
    assert result.ai_request_id
