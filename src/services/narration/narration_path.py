"""Deterministic path-key hashing for narration audio artifacts.

Consumed by `/api/text/narrate-script`. SHA256 over canonical JSON of the
inputs that affect audio generation — same inputs → same hash → same Storage
path (upsert dedupes binary on re-upload).

Hash is computed on `text_with_tags` so cache differentiates per audio-tag
combination (different tags → different audio output).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["build_path_key"]


def build_path_key(
    voice_id: str,
    text_with_tags: str,
    model_id: str,
    settings_dict: dict[str, Any],
    output_format: str,
) -> str:
    """SHA256-hex of canonical-JSON(input) — deterministic across Python runs.

    `settings_dict` must already contain merged defaults (including `seed=None`
    if the caller wants seed to participate in the hash). Key order is
    normalized via `sort_keys=True`.
    """
    payload = {
        "voiceId": voice_id,
        "text": text_with_tags,
        "modelId": model_id,
        "settings": settings_dict,
        "outputFormat": output_format,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
