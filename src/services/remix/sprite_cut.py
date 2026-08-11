"""Cut-only sprite-sheet helper (Phase 02) — pure Pillow, NO AI / NO Replicate.

Runs INSIDE the sprite-sheet-swap job handler AFTER `run_swap_sprite_sheet(
return_bytes=True)` returns a swapped sheet at Gemini-native dim. Unlike the
mix-swap per-crop pipeline (`post_swap_pipeline.py`, job 05 — which also does
remove-bg + upscale), this helper is CUT-ONLY:

  cut     cut_sheet_by_scaled_geometry   Pillow (bytes-only) — N pieces (reused)
  resize  _resize_to_dim_sync            Pillow LANCZOS → ORIGINAL cell geometry
  upload  upload_bytes                   sprite-swap-crops/ (PERMANENT)

`cut_sheet_by_scaled_geometry` already rescales geometry to native dim and crops
N `PieceArtifact.piece_bytes` (RGBA PNG, no Storage upload). This helper resizes
each piece DOWN to its original `crop.geometry` dim (LANCZOS; `_resize_to_dim_sync`
is a no-op when already at target, otherwise resizes — Gemini-native pieces are
larger so this downscales; it never upscales beyond the requested dim) and uploads
the final PNG.

Failure semantics (SHEET-FATAL → `PostSwapPipelineError(code='CUT_FAILED')`):
  - cut raises (empty/decode/all-crops-fail — bubbles up from the reused fn), OR
  - ALL piece uploads fail (empty output).
A SINGLE piece upload failure drops that cell and proceeds when ≥1 piece is OK.

PII discipline: never log URLs / bytes / base64 — only counts + dims + codes.

Spec: `ai-storybook-design/api/jobs/02-enqueue-remix-sprite-swap.md`
§Post-swap Pipeline (CUT ONLY).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.services.remix.post_swap_pipeline import (
    PostSwapPipelineError,
    _now_path_segment,
    _resize_to_dim_sync,
    cut_sheet_by_scaled_geometry,
)
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)

__all__ = ["cut_sprite_sheet_to_crops"]


# Permanent prefix for cut sprite crops inside the shared `storybook-assets` bucket.
STORAGE_SPRITE_CROP_PREFIX = "sprite-swap-crops"


async def cut_sprite_sheet_to_crops(
    sheet_bytes: bytes,
    sheet_geometry: dict[str, int],
    crops: list[dict[str, Any]],
    *,
    sheet_idx: int,
    box_by_index: dict[int, tuple[int, int, int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Cut a swapped sprite sheet into per-cell crops, resize → original geometry, upload.

    Returns `[{type, object_key, variant_key, geometry, media_url}]` — one entry
    per successfully-uploaded cell. `type/object_key/variant_key/geometry` are
    echoed verbatim from the input crop; `media_url` is the uploaded final PNG.

    `box_by_index` (⚡detect-delegate 2026-06-26): forwarded to
    `cut_sheet_by_scaled_geometry` — per-crop boxes from `detect-crop-geometry`
    (the ACTUAL detected frame on the swap result, swapped px) override the static
    `geometry × scale` box per index; missing indices fall back to static
    (graceful). The detected box is the real content region (no border over-crop);
    its aspect ratio ≈ `geometry.w/geometry.h` because the swap preserves the cell
    layout, so the LANCZOS resize back to the original cell dims is essentially a
    downscale (any small ratio drift is normalized to the designed cell). `None`/
    empty → pure static cut.

    Raises `PostSwapPipelineError(code='CUT_FAILED')` when the cut stage fails
    (bubbled from `cut_sheet_by_scaled_geometry`) or when EVERY piece upload
    fails (empty output). A single piece upload failure drops that cell and
    continues when at least one piece succeeds.
    """
    t0 = time.monotonic()

    # 1. Cut — reuse the mix-swap primitive (bytes-only, no Storage upload). Raises
    #    PostSwapPipelineError(code='CUT_FAILED') on empty/decode/all-crops-fail.
    pieces, (w_s, h_s) = await cut_sheet_by_scaled_geometry(
        sheet_bytes, sheet_geometry, crops, sheet_idx=sheet_idx,
        box_by_index=box_by_index,
    )

    # 2. Per piece: resize to ORIGINAL cell geometry dim (LANCZOS) → upload.
    out: list[dict[str, Any]] = []
    upload_fail = 0
    for i, piece in enumerate(pieces):
        geom = piece.crop.get("geometry") or {}
        target_w = max(1, int(geom.get("w", 1)))
        target_h = max(1, int(geom.get("h", 1)))

        final_bytes = await asyncio.to_thread(
            _resize_to_dim_sync, piece.piece_bytes, (target_w, target_h)
        )

        # upload_bytes already wraps the blocking SDK call in asyncio.to_thread
        # (+ transient-transport retry) — no extra to_thread needed here.
        path = f"{STORAGE_SPRITE_CROP_PREFIX}/{_now_path_segment()}-{i}.png"
        try:
            media_url = await upload_bytes(path, final_bytes, content_type="image/png")
        except StorageUploadError as exc:
            upload_fail += 1
            logger.warning(
                "sprite_cut_upload_fail sheet_idx=%d crop_idx=%d reason=%s",
                sheet_idx, i, exc.reason,
            )
            continue

        out.append(
            {
                "type": piece.crop.get("type"),
                "object_key": piece.crop.get("object_key"),
                "variant_key": piece.crop.get("variant_key"),
                "geometry": piece.crop.get("geometry"),
                "media_url": media_url,
            }
        )

    # 3. SHEET-FATAL when no piece uploaded.
    if not out:
        raise PostSwapPipelineError(
            code="CUT_FAILED",
            message="All sprite crop uploads failed",
            details={
                "sheet_idx": sheet_idx,
                "n_pieces": len(pieces),
                "stage": "upload",
            },
        )

    dt_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "sprite_cut_ok sheet_idx=%d W_s=%d H_s=%d crops=%d uploaded=%d upload_fail=%d ms=%d",
        sheet_idx, w_s, h_s, len(pieces), len(out), upload_fail, dt_ms,
    )
    return out
