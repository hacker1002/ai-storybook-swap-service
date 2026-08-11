"""Tile-based upscale helpers (pure CPU, no I/O).

When input exceeds Real-ESRGAN GPU pixel cap (≈2.1MP ~ 1448²), `upscale_core`
splits the source along its long axis into N strip tiles, calls Replicate per
tile in parallel, then `blend_tiles()` recomposes the upscaled strips on a
single canvas with a linear-feather alpha gradient over the
`overlap × scale` output band. Geometry is 1×N or N×1 strip only — grid mode
is intentionally deferred (YAGNI).

All functions here are CPU-bound (Pillow + numpy); callers MUST wrap them in
`asyncio.to_thread`. The module has no awareness of Replicate, HTTP, or I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

import numpy as np
from PIL import Image

Axis = Literal["horizontal", "vertical"]


@dataclass(frozen=True)
class TileBox:
    """Crop rectangle in source-image coordinates plus core-region offset.

    A tile's pixels span `[crop_x, crop_x+crop_w) × [crop_y, crop_y+crop_h)`
    in the source. The "core" sub-region (`core_offset, core_extent`) along
    the split axis identifies which pixels are this tile's "own" output —
    the rest is overlap with neighbors. Flags `is_first`/`is_last` mark edge
    tiles (no overlap on the boundary side).
    """

    crop_x: int
    crop_y: int
    crop_w: int
    crop_h: int
    core_offset: int  # offset within crop, along split axis, where core begins
    core_extent: int  # length of core region along split axis (= input pixels owned)
    is_first: bool
    is_last: bool


def compute_tile_layout(
    src_w: int,
    src_h: int,
    *,
    max_pixels: int,
    overlap: int,
) -> tuple[list[TileBox], Axis]:
    """Choose strip axis + tile count + per-tile crop boxes.

    N is the smallest integer such that every tile (including overlap padding
    on inward sides) fits within `max_pixels`. Strip is along the LONG axis
    so each tile is short along the cut direction and full along the other.

    For inputs ≤ cap, returns a single tile covering the full image with no
    overlap and the long axis chosen by `src_w >= src_h`.
    """
    src_px = src_w * src_h
    axis: Axis = "horizontal" if src_w >= src_h else "vertical"
    long_extent = src_w if axis == "horizontal" else src_h
    short_extent = src_h if axis == "horizontal" else src_w

    if src_px <= max_pixels:
        tile = TileBox(
            crop_x=0,
            crop_y=0,
            crop_w=src_w,
            crop_h=src_h,
            core_offset=0,
            core_extent=long_extent,
            is_first=True,
            is_last=True,
        )
        return [tile], axis

    # Initial ceil; iterate up if overlap padding pushes per-tile area over cap.
    n = max(1, -(-src_px // max_pixels))
    while n <= 64:  # hard sanity bound; caller enforces TILE_MAX_COUNT separately
        core_per_tile = -(-long_extent // n)
        max_crop_long = core_per_tile + 2 * overlap  # interior tile worst case
        if max_crop_long * short_extent <= max_pixels:
            break
        n += 1

    base = long_extent // n
    remainder = long_extent - base * n
    tiles: list[TileBox] = []
    cursor = 0
    for i in range(n):
        core_extent = base + (1 if i < remainder else 0)
        is_first = i == 0
        is_last = i == n - 1
        ov_left = 0 if is_first else overlap
        ov_right = 0 if is_last else overlap
        crop_start = max(0, cursor - ov_left)
        crop_end = min(long_extent, cursor + core_extent + ov_right)
        crop_long = crop_end - crop_start
        core_offset_in_crop = cursor - crop_start

        if axis == "horizontal":
            tile = TileBox(
                crop_x=crop_start,
                crop_y=0,
                crop_w=crop_long,
                crop_h=src_h,
                core_offset=core_offset_in_crop,
                core_extent=core_extent,
                is_first=is_first,
                is_last=is_last,
            )
        else:
            tile = TileBox(
                crop_x=0,
                crop_y=crop_start,
                crop_w=src_w,
                crop_h=crop_long,
                core_offset=core_offset_in_crop,
                core_extent=core_extent,
                is_first=is_first,
                is_last=is_last,
            )
        tiles.append(tile)
        cursor += core_extent

    return tiles, axis


def extract_tile_bytes(src_img: Image.Image, tile: TileBox) -> bytes:
    """Crop `tile` from `src_img` and encode as PNG bytes (lossless)."""
    cropped = src_img.crop(
        (tile.crop_x, tile.crop_y, tile.crop_x + tile.crop_w, tile.crop_y + tile.crop_h)
    )
    if cropped.mode != "RGB":
        cropped = cropped.convert("RGB")
    buf = BytesIO()
    cropped.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _build_feather_mask(
    width: int, height: int, axis: Axis, fade_extent: int
) -> Image.Image:
    """Build an L-mode alpha mask with a linear feather along `axis`.

    For `axis == "horizontal"`, alpha goes 0 → 255 from x=0 to x=fade_extent
    and stays 255 thereafter (used as the LEFT overlap blend of a tile against
    canvas underneath). Mirror for vertical.
    """
    if axis == "horizontal":
        # Per-column alpha ramp 1..fade_extent then 255.
        ramp = np.linspace(1, 255, fade_extent, dtype=np.float32)
        full = np.full(max(0, width - fade_extent), 255.0, dtype=np.float32)
        row = np.concatenate([ramp, full]).clip(0, 255).astype(np.uint8)
        arr = np.broadcast_to(row, (height, width)).copy()
    else:
        ramp = np.linspace(1, 255, fade_extent, dtype=np.float32)
        full = np.full(max(0, height - fade_extent), 255.0, dtype=np.float32)
        col = np.concatenate([ramp, full]).clip(0, 255).astype(np.uint8)
        arr = np.broadcast_to(col.reshape(-1, 1), (height, width)).copy()
    return Image.fromarray(arr, mode="L")


def blend_tiles(
    upscaled_tiles: list[Image.Image],
    layout: list[TileBox],
    *,
    axis: Axis,
    scale: float,
    overlap_input: int,
    output_size: tuple[int, int],
) -> Image.Image:
    """Composite upscaled strip tiles on a single RGB canvas with feather.

    The leftmost (or topmost) tile is pasted verbatim. Each subsequent tile is
    pasted with a linear-feather mask on its inward-facing overlap band so the
    seam fades from 0% (canvas only) to 100% (new tile only) across
    `overlap_input × scale` output pixels.

    `output_size` SHOULD equal `(src_w*scale, src_h*scale)` rounded; caller
    chooses how to round (we round each tile's left/top to `round()`).
    """
    if len(upscaled_tiles) != len(layout):
        raise ValueError(
            f"upscaled_tiles ({len(upscaled_tiles)}) != layout ({len(layout)})"
        )

    canvas = Image.new("RGB", output_size, (0, 0, 0))
    overlap_output = int(round(overlap_input * scale))

    for tile, up_img in zip(layout, upscaled_tiles, strict=True):
        if up_img.mode != "RGB":
            up_img = up_img.convert("RGB")
        crop_w_out, crop_h_out = up_img.size
        if axis == "horizontal":
            tile_left = int(round(tile.crop_x * scale))
            tile_top = 0
        else:
            tile_left = 0
            tile_top = int(round(tile.crop_y * scale))

        if tile.is_first or overlap_output <= 0:
            canvas.paste(up_img, (tile_left, tile_top))
            continue

        fade = min(
            overlap_output,
            crop_w_out if axis == "horizontal" else crop_h_out,
        )
        mask = _build_feather_mask(crop_w_out, crop_h_out, axis, fade)
        canvas.paste(up_img, (tile_left, tile_top), mask)

    return canvas


def encode_png(img: Image.Image) -> bytes:
    """Encode a PIL image to PNG bytes. Lossless, no optimize (faster)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
