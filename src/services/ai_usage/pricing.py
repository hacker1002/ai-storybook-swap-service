"""Cost pricing tables + `compute_cost` for the AI-usage log.

Ported VERBATIM from `ai-storybook-image-api/src/services/ai_usage/pricing.py`
(2026-07-23 pricing table — do NOT alter the numbers; a divergence would misbill
and break cross-service rollup comparability).

ONE source of truth for per-provider USD cost. A model absent from its table is NOT
an error — the row is still logged with `cost_usd=NULL` + a `log.warning`; the price
can be filled later and cost recomputed from `usage_amount` + `pricing_version`
(that is why `PRICING_VERSION` is stamped on every row).

Two intentional divergences from the design doc §Pricing (design is source-of-truth
→ reconciled + ADR-050):
  1. Gemini image-gen bills via `token_table` (input+output tokens), NOT `per_output`.
  2. Replicate community models bill `per_output` flat $/run, NOT `hardware_rate`
     ($/sec) — Replicate publishes only a per-run estimate. `predict_time` is still
     logged in `usage_amount` for observability; cost is flat.

`usage_unit` for Replicate is ALWAYS 'seconds' (never 'images') — the DB CHECK
constraint only allows tokens|seconds|characters, so an 'images' unit would make
the insert FAIL and drop the row.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Bump this whenever any table below changes. Stamped on every row so a past
# cost can be recomputed from usage_amount when prices drift.
PRICING_VERSION = "2026-07-23"

# ── Gemini: ONE token table (text/vision + image-gen). model → per-1M in/out.
# Image-gen differs only in the high output rate (image tokens $120/1M).
# cost = input_tokens/1e6 * in_per_1m + output_tokens/1e6 * out_per_1m.
GEMINI_TOKEN_PRICING: dict[str, dict[str, float]] = {
    "gemini-3-pro-image": {"in_per_1m": 2.00, "out_per_1m": 120.00},  # output = image tokens
    "gemini-3.5-flash": {"in_per_1m": 1.50, "out_per_1m": 9.00},
    "gemini-3-flash-preview": {"in_per_1m": 0.50, "out_per_1m": 3.00},
}

# Fallback fixed token counts when an image-gen response omits usage_metadata.
# input = in_per_ref × num_refs (+ prompt tokens ≈ 0); output by resolution.
GEMINI_IMAGE_FALLBACK_TOKENS: dict[str, dict] = {
    "gemini-3-pro-image": {
        "in_per_ref": 560,
        "out": {"1K": 1120, "2K": 1120, "4K": 2000},
    },
}

# ── Replicate: ALL per_output flat (community + official). cost_source='per_output'.
# usage_unit ALWAYS 'seconds' (log predict_time); cost = rate × num_outputs.
REPLICATE_PER_OUTPUT_PRICING: dict[str, float] = {
    # community (predict_time still logged; cost flat):
    "mattsays/sam3-image": 0.00098,
    "fofr/face-to-many": 0.0087,
    "xinntao/realesrgan": 0.0057,
    "nightmareai/real-esrgan": 0.002,  # per OUTPUT IMAGE (× num_outputs)
    "alexgenovese/upscaler": 0.0058,
    "851-labs/background-remover": 0.00049,
    # official:
    "bria/remove-background": 0.018,
    "recraft-ai/recraft-crisp-upscale": 0.006,
    "flux-kontext-apps/text-removal": 0.04,
    # "qwen/qwen-image-layered": NO published price → miss → costUsd=None + warn
}

# ── ElevenLabs: USD per 1,000 characters. cost_source='char_rate'.
ELEVENLABS_CHAR_PRICING: dict[str, float] = {
    "eleven_v3": 0.10,
    "eleven_turbo_v2_5": 0.05,
    "eleven_multilingual_v2": 0.10,
}

_DEFAULT_RESOLUTION = "2K"


def _unknown(provider: str, model: str | None) -> dict:
    """A model with no price row — log usage, cost NULL, warn (never drops the row)."""
    logger.warning(
        "ai_usage_pricing_unknown provider=%s model=%s (row logged, cost_usd=NULL)",
        provider, model,
    )
    return {"costUsd": None, "costSource": "unknown", "pricingVersion": PRICING_VERSION}


def compute_cost(provider: str, model: str | None, usage: dict | None = None) -> dict:
    """Return `{costUsd, costSource, pricingVersion}` for one provider call.

    `usage` keys by provider:
      - gemini    : {input_tokens?, output_tokens?, num_refs?, resolution?}
        (missing tokens on an image model → fixed-token fallback)
      - replicate : {seconds?, num_outputs?} (seconds is log-only; cost = rate × outputs)
      - elevenlabs: {characters}

    A model missing from its table → costUsd=None, costSource='unknown', + warn.
    """
    usage = usage or {}

    if provider == "gemini":
        rates = GEMINI_TOKEN_PRICING.get(model or "")
        if rates is None:
            return _unknown(provider, model)
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        if in_tok is None and out_tok is None:
            fb = GEMINI_IMAGE_FALLBACK_TOKENS.get(model or "")
            if fb is not None:
                num_refs = usage.get("num_refs", 1) or 1
                resolution = usage.get("resolution") or _DEFAULT_RESOLUTION
                in_tok = fb["in_per_ref"] * num_refs
                out_tok = fb["out"].get(resolution, fb["out"][_DEFAULT_RESOLUTION])
            else:
                # text/vision with no usage_metadata → cannot bill; warn but keep row.
                logger.warning(
                    "ai_usage_pricing_no_tokens provider=gemini model=%s "
                    "(no usage_metadata, no fallback → cost_usd=NULL)", model,
                )
                return {"costUsd": None, "costSource": "token_table", "pricingVersion": PRICING_VERSION}
        cost = (in_tok or 0) / 1e6 * rates["in_per_1m"] + (out_tok or 0) / 1e6 * rates["out_per_1m"]
        return {"costUsd": round(cost, 6), "costSource": "token_table", "pricingVersion": PRICING_VERSION}

    if provider == "replicate":
        rate = REPLICATE_PER_OUTPUT_PRICING.get(model or "")
        if rate is None:
            return _unknown(provider, model)
        num_outputs = usage.get("num_outputs", 1) or 1
        cost = rate * num_outputs
        return {"costUsd": round(cost, 6), "costSource": "per_output", "pricingVersion": PRICING_VERSION}

    if provider == "elevenlabs":
        rate = ELEVENLABS_CHAR_PRICING.get(model or "")
        if rate is None:
            return _unknown(provider, model)
        chars = usage.get("characters", 0) or 0
        cost = rate * chars / 1000.0
        return {"costUsd": round(cost, 6), "costSource": "char_rate", "pricingVersion": PRICING_VERSION}

    return _unknown(provider, model)
