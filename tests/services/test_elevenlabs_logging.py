"""`elevenlabs_client._log_elevenlabs_call` — choke-point entry construction.

Regression for the P3b port miss where the image-api call shape
(`AiLogEntry(id=new_request_id(), ...)`) survived the port while this service's
`AiLogEntry` has NO `id` field (DB mints `ai_service_logs.id`) — every ElevenLabs
call in the audio-swap job then raised
`AiLogEntry.__init__() got an unexpected keyword argument 'id'` at the log line.
Captures the entry by stubbing `log_ai_request`, so construction runs for real.
"""

from __future__ import annotations

from src.services import elevenlabs_client as ec
from src.services.ai_usage import AiCallContext, AiLogEntry


def _capture(monkeypatch):
    captured: list[AiLogEntry] = []
    monkeypatch.setattr(ec, "log_ai_request", captured.append)
    return captured


def test_log_elevenlabs_call_constructs_entry(monkeypatch):
    captured = _capture(monkeypatch)
    ec._log_elevenlabs_call(
        ctx=AiCallContext(remix_id="00000000-0000-0000-0000-000000000001"),
        operation="narrate-script",
        model_id="eleven_v3",
        char_count=42,
        request_payload={"text": "hello"},
        provider_request_id="req-1",
    )
    assert len(captured) == 1
    entry = captured[0]
    assert entry.provider == "elevenlabs"
    assert entry.operation == "narrate-script"
    assert entry.status == "success"
    assert entry.usage_unit == "characters" and entry.usage_amount == 42
    assert entry.id  # log-time minted row id (audio is LOG-ONLY, no envelope)


def test_log_elevenlabs_call_error_path_no_cost(monkeypatch):
    captured = _capture(monkeypatch)
    ec._log_elevenlabs_call(
        ctx=None,
        operation="narrate-script",
        model_id="eleven_v3",
        char_count=42,
        request_payload={"text": "hello"},
        status="error",
        error=RuntimeError("boom"),
    )
    entry = captured[0]
    assert entry.status == "error" and entry.error == "boom"
    assert entry.cost is None  # provider did not charge on the error path
