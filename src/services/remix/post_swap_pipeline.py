"""Post-swap pipeline (⚡rev9 2026-06-12 — CUT ONLY) — pure Pillow helpers.

Runs INSIDE the mix-swap job handler (`jobs/handlers/remix_mix_swap.py::do_sheet`)
AFTER `run_swap_mix_sheet()` returns a swapped sheet at Gemini-native dim. rev9
shrinks the rev7 per-crop pipeline (cut → per-crop[remove-bg → upscale]) to
CUT-ONLY: remove-bg and upscale are now standalone stage jobs writing their own
JSONB columns — job 09 `remix_rmbg` (`rmbgs[]`) + job 10 `remix_upscale`
(`upscales[]`). This module no longer calls Replicate at all.

  cut     cut_sheet_by_scaled_geometry   Pillow (bytes-only) — N pieces
  upload  cut_and_upload_native          upload each piece AT NATIVE DIM
                                         (NO resize) → permanent prefix

Native-dim contract (⚡locked 2026-06-12): the cut pieces keep the Gemini-native
resolution — `geometry × (W_swap / sheet_geometry.width)` — and are uploaded
verbatim. UNLIKE sprite_cut (job 02, resize-to-geometry — terminal output),
mix pieces feed the rmbg/upscale stages downstream, so resolution is preserved
end-to-end. ⚠️ "Native" can be SMALLER than `geometry` (Gemini caps output at
~2K; a multi-crop sheet with `sheet_geometry` > 2K scales pieces down at swap
time) — native means no fake resolution via resize, not native ≥ geometry.

Lean output (⚡DB lean shape 2026-06-12): each uploaded piece is returned as
`{spread_id, id, media_url}` — no geometry/tags (readers join `original_crops[]`
by `(spread_id, id)`).

The shared cut helper (`cut_sheet_by_scaled_geometry`) + Pillow sync helpers
(`_resize_to_dim_sync`, `_measure_sync`, `_now_path_segment`) stay here — reused
by `sprite_cut.py` (job 02) and the rmbg/upscale stage jobs (09/10).

All Pillow ops are wrapped in `asyncio.to_thread` (blocking C-extension). PII
discipline: never log URLs / bytes / base64 — only counts, dims, codes.

Spec: `ai-storybook-design/api/jobs/05-enqueue-remix-mix-swap.md` §Post-swap
Pipeline — CUT ONLY (⚡rev9).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from PIL import Image

from src.services import image_ops  # noqa: F401 — Pillow setup side-effects
from src.services.storage import StorageUploadError, upload_bytes

logger = logging.getLogger(__name__)


__all__ = [
    "PostSwapPipelineError",
    "PieceArtifact",
    "cut_sheet_by_scaled_geometry",
    "cut_and_upload_native",
    "STORAGE_FINAL_CROP_PREFIX",
]


# ─── Storage paths ─────────────────────────────────────────────────────────

STORAGE_FINAL_CROP_PREFIX = "post-swap-final"


# ─── Errors ────────────────────────────────────────────────────────────────


class PostSwapPipelineError(Exception):
    """Raised by pipeline stages — handler catches per-stage and isolates.

    ⚡rev9: the only sheet-fatal `code` left in this module is `CUT_FAILED`
    (`models.jobs.remix_mix_swap.PIPELINE_ERROR_CODES`). The rev7 per-crop
    codes (REMOVE_BG_*, UPSCALE_FAILED, ALL_CROP_PIPELINES_FAILED) moved with
    their stages to jobs 09/10.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}


# ─── Dataclasses ───────────────────────────────────────────────────────────


@dataclasses.dataclass(slots=True)
class PieceArtifact:
    """Output of cut — one cropped piece at scaled (Gemini-native) geometry.

    `crop` is the ORIGINAL input crop dict (`{spread_id, id, geometry, ...}`
    per the job handler context). `new_geom` is the rescaled geometry at
    native dim. `piece_bytes` is PNG-encoded RGBA — kept in-memory (no Storage
    upload at cut time).
    """

    crop: dict[str, Any]
    new_geom: dict[str, int]
    piece_bytes: bytes
    # ⚡detect-delegate (2026-06-26): True = box came from detect-crop-geometry
    # (API located the cell), False/None = static `geometry × scale` fallback.
    detected: bool | None = None


# ─── Internal helpers ──────────────────────────────────────────────────────


def _now_path_segment() -> str:
    """`{YYYY-MM-DD}/{ts_ms}-{uuid_hex}` — keeps writes sortable + unique."""
    now = datetime.now(timezone.utc)
    ts_ms = int(time.time() * 1000)
    return f"{now:%Y-%m-%d}/{ts_ms}-{uuid.uuid4().hex}"


def _crop_piece_sync(sheet_bytes: bytes, new_geom: dict[str, int]) -> bytes:
    """Decode sheet, crop one piece at `new_geom`, return PNG bytes (RGBA)."""
    with Image.open(BytesIO(sheet_bytes)) as src:
        src.load()
        rgba = src.convert("RGBA") if src.mode != "RGBA" else src
        try:
            x, y, w, h = new_geom["x"], new_geom["y"], new_geom["w"], new_geom["h"]
            box = (x, y, x + w, y + h)
            piece = rgba.crop(box)
            try:
                buf = BytesIO()
                piece.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
            finally:
                piece.close()
        finally:
            if rgba is not src:
                rgba.close()


def _measure_sync(image_bytes: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(image_bytes)) as src:
        return src.size  # (w, h)


def _resize_to_dim_sync(image_bytes: bytes, target_dim: tuple[int, int]) -> bytes:
    """Decode, LANCZOS-resize to exact target_dim (RGBA), return PNG bytes.

    NOT used by the rev9 mix cut (native dim is kept verbatim) — retained for
    `sprite_cut.py` (job 02 resize-to-geometry) and the upscale stage job (10,
    resize-to-exact print dims). Forces RGBA to preserve alpha. No-op resize is
    skipped when already at target_dim.
    """
    with Image.open(BytesIO(image_bytes)) as src:
        src.load()
        rgba = src.convert("RGBA") if src.mode != "RGBA" else src
        try:
            if rgba.size != target_dim:
                out = rgba.resize(target_dim, Image.Resampling.LANCZOS)
            else:
                out = rgba
            try:
                buf = BytesIO()
                out.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
            finally:
                if out is not rgba:
                    out.close()
        finally:
            if rgba is not src:
                rgba.close()


def _static_box(
    geom: dict[str, Any], scale_x: float, scale_y: float
) -> tuple[int, int, int, int]:
    """Static `geometry × scale` box (`ex, ey, ew, eh`) — the pre-refine cut box
    and the border-detect `expected`/fallback. Mirrors the legacy inline math so
    the non-refine path stays byte-identical."""
    gx = int(geom.get("x", 0))
    gy = int(geom.get("y", 0))
    gw = int(geom.get("w", 0))
    gh = int(geom.get("h", 0))
    return (
        max(0, round(gx * scale_x)),
        max(0, round(gy * scale_y)),
        max(1, round(gw * scale_x)),
        max(1, round(gh * scale_y)),
    )


def _crop_id_or_fallback(crop: dict[str, Any], idx: int) -> str:
    raw = crop.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"crop-{idx}"


# ─── Stage: cut ────────────────────────────────────────────────────────────


async def cut_sheet_by_scaled_geometry(
    sheet_bytes: bytes,
    sheet_geometry: dict[str, int],
    crops: list[dict[str, Any]],
    *,
    sheet_idx: int,
    box_by_index: dict[int, tuple[int, int, int, int]] | None = None,
) -> tuple[list[PieceArtifact], tuple[int, int]]:
    """cut — decode swapped sheet bytes, rescale geometry to native dim, crop N pieces.

    Bytes-only signature (Approach B+, 2026-05-28): the primitive 04 caller
    passes `SwapMixSheetCoreResult.image_bytes` directly; no URL fetch (was
    tripping the 10 MB `fetch_image_bytes` cap on Gemini-native 4K sheets).

    `box_by_index` (⚡detect-delegate 2026-06-26): per-crop boxes in SWAPPED-image
    px from `detect-crop-geometry` — each is the ACTUAL detected inner-of-stroke
    frame on the swap result (no ratio reshape, so the cut never spills into the
    border/gutter). For crop index `i` present in the map → that box is used
    VERBATIM (already on the swapped sheet, no scale). Index missing (API notFound
    / API failed) → static
    `geometry × scale` fallback (graceful, never-worse). `None`/empty map → the
    pure static path for callers that never delegate (e.g. the borderless rmbg
    sheet in job 09).

    Returns (pieces, (W_s, H_s)) where `pieces[i].piece_bytes` is PNG RGBA. NO
    Storage upload (bytes handoff to the caller). Raises
    `PostSwapPipelineError(code='CUT_FAILED')` on any failure (empty input,
    decode, crop, or zero successful pieces).
    """
    n_crops = len(crops)
    t0 = time.monotonic()
    detected_boxes = box_by_index or {}

    # 1. Defensive: bytes must be non-empty (handler<>core contract — primitive
    #    04 with return_bytes=True always populates image_bytes).
    if not sheet_bytes:
        raise PostSwapPipelineError(
            code="CUT_FAILED",
            message="Sheet bytes are empty (handler<>core contract violated)",
            details={"sheet_idx": sheet_idx, "stage": "input_missing"},
        )

    # 2. Measure actual (W_s, H_s).
    try:
        W_s, H_s = await asyncio.to_thread(_measure_sync, sheet_bytes)
    except Exception as exc:
        logger.warning(
            "post_swap_cut_measure_fail sheet_idx=%d err_type=%s",
            sheet_idx, type(exc).__name__,
        )
        raise PostSwapPipelineError(
            code="CUT_FAILED",
            message="Failed to decode swapped sheet",
            details={"sheet_idx": sheet_idx, "stage": "measure"},
        ) from exc

    target_w = int(sheet_geometry.get("width") or 0)
    target_h = int(sheet_geometry.get("height") or 0)
    if target_w <= 0 or target_h <= 0:
        raise PostSwapPipelineError(
            code="CUT_FAILED",
            message="Invalid sheet_geometry (width/height must be > 0)",
            details={"sheet_idx": sheet_idx},
        )

    scale_x = W_s / target_w
    scale_y = H_s / target_h

    # 3. Crop each piece — detected box (swapped px) when available, else static
    #    `geometry × scale`. Fail-fast if all crops fail; log + skip on single
    #    crop fail and continue (rare — bad geometry).
    pieces: list[PieceArtifact] = []
    crop_fail = 0
    detected_count = 0
    for idx, crop in enumerate(crops):
        detected_box = detected_boxes.get(idx)
        if detected_box is not None:
            bx, by, bw, bh = detected_box
            detected: bool | None = True
            detected_count += 1
        else:
            bx, by, bw, bh = _static_box(crop.get("geometry") or {}, scale_x, scale_y)
            detected = False if detected_boxes else None

        new_geom = {"x": max(0, int(bx)), "y": max(0, int(by)), "w": max(1, int(bw)), "h": max(1, int(bh))}
        # Clamp to sheet bounds.
        new_geom["w"] = min(new_geom["w"], max(1, W_s - new_geom["x"]))
        new_geom["h"] = min(new_geom["h"], max(1, H_s - new_geom["y"]))

        try:
            piece_bytes = await asyncio.to_thread(
                _crop_piece_sync, sheet_bytes, new_geom
            )
        except Exception as exc:
            crop_fail += 1
            logger.warning(
                "post_swap_cut_crop_fail sheet_idx=%d crop_idx=%d err_type=%s",
                sheet_idx, idx, type(exc).__name__,
            )
            continue

        pieces.append(
            PieceArtifact(
                crop=crop, new_geom=new_geom, piece_bytes=piece_bytes, detected=detected
            )
        )

    if not pieces:
        raise PostSwapPipelineError(
            code="CUT_FAILED",
            message="All piece crops failed",
            details={"sheet_idx": sheet_idx, "n_crops": n_crops},
        )

    dt_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "post_swap_cut_ok sheet_idx=%d W_s=%d H_s=%d pieces=%d skipped=%d "
        "detected=%d static=%d ms=%d",
        sheet_idx, W_s, H_s, len(pieces), crop_fail,
        detected_count, len(pieces) - detected_count, dt_ms,
    )
    return pieces, (W_s, H_s)


# ─── Stage: cut + upload native (⚡rev9) ────────────────────────────────────


async def cut_and_upload_native(
    sheet_bytes: bytes,
    sheet_geometry: dict[str, int],
    crops: list[dict[str, Any]],
    *,
    sheet_idx: int,
    storage_prefix: str = STORAGE_FINAL_CROP_PREFIX,
    box_by_index: dict[int, tuple[int, int, int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Cut a swapped sheet into N pieces and upload each AT NATIVE DIM (no resize).

    ⚡rev9 (2026-06-12): the mix pieces feed the rmbg/upscale stage jobs, so the
    Gemini-native resolution is preserved verbatim — `piece_bytes` is uploaded
    exactly as cut (UNLIKE `sprite_cut.py` which resizes back to the cell
    geometry). Also reused by the rmbg stage job (09) with
    `storage_prefix='rmbg-final'` — there the composed sheet matches
    `sheet_geometry` (scale ≈ 1) and the same rescale logic degrades to a
    straight cut, doubling as the defensive rescale for dim drift.

    Returns the LEAN crop entries `[{spread_id, id, media_url}]` (geometry/tags
    are joined from `original_crops[]` by the reader — DB lean 2026-06-12).

    Failure semantics:
      - cut raises → bubbles `PostSwapPipelineError(code='CUT_FAILED')`.
      - single piece upload fail → 1 inline retry → drop the piece (siblings
        proceed when ≥1 piece is OK).
      - ALL piece uploads fail → `PostSwapPipelineError(code='CUT_FAILED')`.
    """
    t0 = time.monotonic()

    pieces, (w_s, h_s) = await cut_sheet_by_scaled_geometry(
        sheet_bytes, sheet_geometry, crops, sheet_idx=sheet_idx,
        box_by_index=box_by_index,
    )

    out: list[dict[str, Any]] = []
    upload_fail = 0
    for i, piece in enumerate(pieces):
        crop_id = _crop_id_or_fallback(piece.crop, i)
        media_url: str | None = None
        for attempt in (1, 2):  # 1 inline retry per spec 05 §cut
            # Fresh path per attempt — avoids an already-exists conflict when
            # the first attempt failed after the object was created.
            path = f"{storage_prefix}/{_now_path_segment()}-{sheet_idx}-{crop_id}.png"
            try:
                media_url = await upload_bytes(
                    path, piece.piece_bytes, content_type="image/png"
                )
                break
            except StorageUploadError as exc:
                logger.warning(
                    "post_swap_native_upload_fail sheet_idx=%d crop_idx=%d attempt=%d reason=%s",
                    sheet_idx, i, attempt, exc.reason,
                )
        if media_url is None:
            upload_fail += 1
            continue

        out.append(
            {
                "spread_id": piece.crop.get("spread_id"),
                "id": crop_id,
                "media_url": media_url,
            }
        )

    if not out:
        raise PostSwapPipelineError(
            code="CUT_FAILED",
            message="All native piece uploads failed",
            details={
                "sheet_idx": sheet_idx,
                "n_pieces": len(pieces),
                "stage": "upload",
            },
        )

    dt_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "post_swap_native_cut_ok sheet_idx=%d W_s=%d H_s=%d pieces=%d uploaded=%d upload_fail=%d ms=%d",
        sheet_idx, w_s, h_s, len(pieces), len(out), upload_fail, dt_ms,
    )
    return out
