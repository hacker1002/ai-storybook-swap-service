"""Pure string path builders for Supabase Storage keys (no I/O) — REMIX DOMAIN ONLY.

Ported from image-api `services/storage/paths.py` (audio + sanitizers) and
`services/remix/swap_image_helpers.py` (`build_dated_path`). Per the P3b phase-01
spec this keeps ONLY the remix-pipeline + audio builders; every sketch /
illustration / scene / segment / edit / background / outpaint / normalize /
spread-thumbnail builder is intentionally DROPPED (YAGNI — not on the remix path).

The remix crop-pipeline stages (swap → rmbg → upscale, plus sprite) all address
their outputs via `build_dated_path(<STAGE_PREFIX>)`; the prefix constants below
are the same string literals image-api's handlers hardcode.
"""

import re
import time
import uuid
from datetime import datetime, timezone

_FILENAME_SAFE_RE = re.compile(r"[^a-z0-9_-]")

# ── Remix crop-pipeline stage prefixes (parity w/ image-api handler constants) ──
# swap (04 mix) / (03 sprite)
STORAGE_MIX_SWAP_PREFIX = "crop-sheet-swaps"
STORAGE_MIX_COMPOSED_PREFIX = "crop-sheet-composed"
STORAGE_VARIANT_SHEET_PREFIX = "variant-sheets"
STORAGE_SPRITE_SWAP_PREFIX = "sprite-sheet-swaps"
STORAGE_SPRITE_COMPOSED_PREFIX = "sprite-sheet-composed"
STORAGE_SPRITE_CROP_PREFIX = "sprite-swap-crops"
STORAGE_POST_SWAP_FINAL_PREFIX = "post-swap-final"
# rmbg (09)
STORAGE_RMBG_SHEET_PREFIX = "rmbg-sheets"
STORAGE_RMBG_CROP_PREFIX = "rmbg-final"
# upscale (10)
STORAGE_UPSCALE_CROP_PREFIX = "upscale-final"

# ── Audio prefixes ──
_VOICE_PREVIEW_PREFIX = "voices/previews"
_NARRATION_PREFIX = "narrations"


def sanitize_filename(raw: str, max_len: int = 20) -> str:
    s = raw.lower()
    s = _FILENAME_SAFE_RE.sub("_", s)
    s = s.strip("_") or "image"
    return s[:max_len]


def build_dated_path(prefix: str) -> str:
    """`{prefix}/{YYYY-MM-DD}/{uuid_hex}-{ts_ms}.png` — no PII, just sortable.

    The single remix-pipeline path builder (crop sheet / sprite / rmbg / upscale
    outputs). Ported verbatim from image-api `swap_image_helpers.build_dated_path`.
    """
    ts = int(time.time() * 1000)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix}/{date}/{uuid.uuid4().hex}-{ts}.png"


# ── Audio path builders (remix audio-swap outputs) ──


def build_narration_path(path_key: str, ext: str = "mp3") -> str:
    """Deterministic narration path: `narrations/{sha256_hex}.{ext}`.

    Same input → same path, so upsert=True dedupes by overwriting the identical
    (deterministic-seed) audio.
    """
    return f"{_NARRATION_PREFIX}/{path_key}.{ext}"


def build_combined_narration_path(path_key: str) -> str:
    """`narrations/combined/{sha256_hex}.mp3` — deterministic upsert dedup."""
    return f"{_NARRATION_PREFIX}/combined/{path_key}.mp3"


def build_voice_preview_path(eleven_id: str) -> str:
    """`voices/previews/{ts_ms}-{eleven_id}.mp3`."""
    ts_ms = int(time.time() * 1000)
    safe_id = _FILENAME_SAFE_RE.sub("_", eleven_id.lower())[:40]
    return f"{_VOICE_PREVIEW_PREFIX}/{ts_ms}-{safe_id}.mp3"
