"""Compat shim: image-api's `src.services.storage` upload seam over this service's
`AppStorageAdapter` (`src/storage/`).

Ported remix cores import `from src.services.storage import StorageUploadError,
upload_bytes` VERBATIM. image-api exposes these as module functions over its
Supabase-SDK uploader; this service routes them through the single `get_storage()`
adapter seam (Supabase Storage REST, NO SDK) so the cores stay byte-identical while
every write goes through the one swappable Storage surface. `StorageUploadError` is
re-exported from the concrete adapter so the cores' `except StorageUploadError`
paths catch the real failure raised by `get_storage().upload(...)`.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from src.storage.adapter import get_storage
from src.storage.paths import sanitize_filename
from src.storage.supabase_rest import StorageUploadError

__all__ = [
    "StorageUploadError",
    "upload_bytes",
    "build_remove_bg_path",
    "build_upscale_path",
    "build_narration_path",
    "build_combined_narration_path",
]

# Permanent prefixes inside the shared bucket (parity with image-api storage/paths).
_REMOVE_BG_PREFIX = "remove-bg-objects"
_UPSCALE_PREFIX = "upscale"
_NARRATION_PREFIX = "narrations"


def build_remove_bg_path(image_url: str) -> str:
    """image-remove-bg output path: `remove-bg-objects/{ts_ms}-{slug}-nobg.png`.

    Ported from image-api `storage/paths.build_remove_bg_path`. Only the core's
    URL-upload branch (return_bytes=False) uses it — the `remix_rmbg` handler goes
    through return_bytes=True, so this is import-resolution parity only."""
    timestamp_ms = int(time.time() * 1000)
    parsed = urlparse(image_url)
    base = parsed.path.rsplit("/", 1)[-1] or "image"
    if "." in base:
        base = base.rsplit(".", 1)[0]
    origin_name = sanitize_filename(base, max_len=20)
    return f"{_REMOVE_BG_PREFIX}/{timestamp_ms}-{origin_name}-nobg.png"


def build_upscale_path(origin_name: str, scale: float) -> str:
    """upscale-image output path: `upscale/{ts_ms}-{slug20}-x{scale_slug}.png`.

    Ported from image-api `storage/paths.build_upscale_path`. `scale_slug` uses
    `"%g"` with `.`→`_` (4.0→x4, 2.5→x2_5, 10.0→x10)."""
    timestamp_ms = int(time.time() * 1000)
    slug = sanitize_filename(origin_name or "image", max_len=20)
    scale_slug = ("%g" % scale).replace(".", "_")
    return f"{_UPSCALE_PREFIX}/{timestamp_ms}-{slug}-x{scale_slug}.png"


def build_narration_path(path_key: str, ext: str = "mp3") -> str:
    """Deterministic narration path: `narrations/{sha256_hex}.{ext}`.

    Ported verbatim from image-api `storage/paths.build_narration_path` (pure
    string builder, no I/O). path_key is the SHA256 hex from
    `narration_path.build_path_key`. Same input -> same path, so upsert=True
    dedupes by overwriting the identical (deterministic-seed) audio.
    """
    return f"{_NARRATION_PREFIX}/{path_key}.{ext}"


def build_combined_narration_path(path_key: str) -> str:
    """Path for `/api/text/combine-audio-chunks` output:
    `narrations/combined/{sha256_hex}.mp3`.

    Ported verbatim from image-api `storage/paths.build_combined_narration_path`
    (pure string builder, no I/O). path_key derives from canonical request hash
    -> deterministic upsert dedup.
    """
    return f"{_NARRATION_PREFIX}/combined/{path_key}.mp3"


async def upload_bytes(
    path: str,
    body: bytes,
    content_type: str,
    bucket: str | None = None,
    upsert: bool = True,
) -> str:
    """Upload arbitrary bytes → return the object's public URL.

    Parity wrapper for image-api's `storage.upload_bytes`. Delegates to the adapter
    (`get_storage().upload`) which is already async (wraps the blocking httpx PUT
    with retry) and raises `StorageUploadError` on a non-transport failure /
    exhausted retries — the same exception the cores catch.
    """
    return await get_storage().upload(
        path, body, content_type, bucket=bucket, upsert=upsert
    )
