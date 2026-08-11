"""pricing.py — spot-check the versioned table ported verbatim (no number drift)."""

from __future__ import annotations

from src.services.ai_usage.pricing import PRICING_VERSION, compute_cost


def test_pricing_version_pinned():
    assert PRICING_VERSION == "2026-07-23"


def test_gemini_token_cost():
    out = compute_cost("gemini", "gemini-3-pro-image", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    # 1M in × $2 + 1M out × $120 = 122.0
    assert out["costUsd"] == 122.0
    assert out["costSource"] == "token_table"
    assert out["pricingVersion"] == "2026-07-23"


def test_gemini_image_fallback_tokens_when_usage_missing():
    out = compute_cost("gemini", "gemini-3-pro-image", {"num_refs": 2, "resolution": "2K"})
    # in = 560×2 = 1120 → 1120/1e6×2 ; out = 1120 → 1120/1e6×120
    assert out["costUsd"] == round(1120 / 1e6 * 2.0 + 1120 / 1e6 * 120.0, 6)


def test_replicate_per_output_flat():
    out = compute_cost("replicate", "bria/remove-background", {"seconds": 9.9, "num_outputs": 2})
    assert out["costUsd"] == round(0.018 * 2, 6)
    assert out["costSource"] == "per_output"


def test_elevenlabs_char_rate():
    out = compute_cost("elevenlabs", "eleven_v3", {"characters": 500})
    assert out["costUsd"] == round(0.10 * 500 / 1000.0, 6)
    assert out["costSource"] == "char_rate"


def test_unknown_model_logs_null_cost_not_error():
    out = compute_cost("replicate", "made/up-model", {"num_outputs": 1})
    assert out["costUsd"] is None and out["costSource"] == "unknown"
