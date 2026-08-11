"""⚡rev6 variant-sheet composer for the multi-target mix swap (spec 04).

Builds the two MIRRORED appearance sheets (old-variant + new-variant) the mix
core sends to Gemini alongside the crop sheet. The layout is computed ONCE per
request (`compute_variant_sheet_layout`) and shared by both sheets — the mirror
invariant (cell i has IDENTICAL geometry + ordinal on both sheets) is therefore
structural, not coincidental.

Rendering REUSES the field-proven `01` frame conventions (gutter, stroke,
ordinal badge) via `crop_sheet_composer._compose_sync` with the ⚡rev6
`fit_mode="contain"` branch: variant slots are uniform 768px squares while the
source images have arbitrary aspect, so the image is aspect-preserved and
centered with gutter-colored slack (crop sheets keep their default stretch).

The composer NEVER fetches URLs — it receives bytes the core already fetched
through the SSRF-guarded seam. A cell that fails to decode raises
`VariantCellDecodeError(index)` so the core can map it onto the right
target_key / error code (REFERENCE_FETCH_ERROR vs TARGET_BASE_FETCH_ERROR).
"""

from __future__ import annotations

import asyncio
import dataclasses
import math

from src.models.requests.build_crop_sheet import (
    BuildCropSheetRequest,
    Crop,
    FrameStyle,
    Geometry,
    SheetGeometry,
)
from src.services.gemini.payload_budget import VARIANT_CELL_EDGE, VARIANT_SHEET_COLS
from src.services.remix.crop_sheet_composer import _compose_sync, _decode_one

__all__ = [
    "VARIANT_MARGIN_LEFT",
    "VARIANT_MARGIN_RIGHT",
    "VARIANT_MARGIN_TOP",
    "VARIANT_MARGIN_BOTTOM",
    "VARIANT_GAP_X",
    "VARIANT_GAP_Y",
    "VariantCellDecodeError",
    "VariantSheetLayout",
    "compute_variant_sheet_layout",
    "compose_variant_sheet",
]

# Frame metrics — reuse the `01` conventions, with the top-gutter inversion locked
# in plan 260630 (badge moved LEFT→TOP gutter): top margin 64px + vertical gap 64px
# so the ordinal badge (HEIGHT cap 50px, `crop_sheet_composer._MAX_BADGE_H`) ALWAYS
# fits the gutter ABOVE each cell — the no-overlap strict-top placement. The
# vertical gap 64 doubles as the top gutter of rows 2..N; horizontal gap shrinks to
# 8px (crops sit side-by-side) since the badge no longer needs a left gutter.
VARIANT_MARGIN_LEFT = 4
VARIANT_MARGIN_RIGHT = 4
VARIANT_MARGIN_TOP = 64
VARIANT_MARGIN_BOTTOM = 16
VARIANT_GAP_X = 8
VARIANT_GAP_Y = 64

# Placeholder URL for the synthetic Crop models — `_compose_sync` never fetches
# (items are pre-decoded), but the Crop schema requires an http(s) URL shape.
_PLACEHOLDER_URL = "https://variant.local/cell"


class VariantCellDecodeError(Exception):
    """One variant image failed to decode. `index` = position in the input list
    (== swap_targets index) so the core can attribute the failure to a target."""

    def __init__(self, index: int) -> None:
        super().__init__(f"variant cell {index} failed to decode")
        self.index = index


@dataclasses.dataclass(frozen=True, slots=True)
class VariantSheetLayout:
    """Shared geometry of BOTH variant sheets (mirror invariant by construction).

    `cells` is row-major, len == N; `cells[i]` is the slot of target i on both
    the old and the new sheet. Cell numbers (ordinal badges) are `i + 1`,
    matching `variant_manifest[].number`.
    """

    cells: list[Geometry]
    sheet_w: int
    sheet_h: int


def compute_variant_sheet_layout(n: int) -> VariantSheetLayout:
    """Grid layout for N targets: cols=min(N, 5), rows=ceil(N/cols), uniform
    square `VARIANT_CELL_EDGE` slots, row-major fill. Runs ONCE per request —
    both sheets compose from the SAME returned layout."""
    if n < 1:
        raise ValueError(f"variant sheet needs at least 1 target, got {n}")
    cols = min(n, VARIANT_SHEET_COLS)
    rows = math.ceil(n / cols)
    edge = VARIANT_CELL_EDGE
    sheet_w = (
        VARIANT_MARGIN_LEFT
        + cols * edge
        + (cols - 1) * VARIANT_GAP_X
        + VARIANT_MARGIN_RIGHT
    )
    sheet_h = (
        VARIANT_MARGIN_TOP
        + rows * edge
        + (rows - 1) * VARIANT_GAP_Y
        + VARIANT_MARGIN_BOTTOM
    )
    cells = [
        Geometry(
            x=VARIANT_MARGIN_LEFT + (i % cols) * (edge + VARIANT_GAP_X),
            y=VARIANT_MARGIN_TOP + (i // cols) * (edge + VARIANT_GAP_Y),
            w=edge,
            h=edge,
        )
        for i in range(n)
    ]
    return VariantSheetLayout(cells=cells, sheet_w=sheet_w, sheet_h=sheet_h)


async def compose_variant_sheet(
    images: list[bytes], layout: VariantSheetLayout
) -> bytes:
    """Compose ONE variant sheet (PNG bytes) from pre-fetched image bytes.

    `images[i]` lands in `layout.cells[i]` (fit-contain, centered) with the
    ordinal badge `i + 1` baked by the shared `01` renderer. Raises
    `VariantCellDecodeError(i)` on a non-decodable image; ValueError on a
    count/layout mismatch (caller bug — fail loud).
    """
    if len(images) != len(layout.cells):
        raise ValueError(
            f"images count {len(images)} != layout cells {len(layout.cells)}"
        )

    decoded = []
    for i, data in enumerate(images):
        img = await asyncio.to_thread(_decode_one, data)
        if img is None:
            for prev, _skip in decoded:
                if prev is not None:
                    prev.close()
            raise VariantCellDecodeError(i)
        decoded.append((img, None))

    req = BuildCropSheetRequest(
        sheet_geometry=SheetGeometry(width=layout.sheet_w, height=layout.sheet_h),
        crops=[
            Crop(id=f"cell-{i + 1}", media_url=_PLACEHOLDER_URL, geometry=cell)
            for i, cell in enumerate(layout.cells)
        ],
        frame=FrameStyle(),  # 01 defaults: white gutter, black stroke 4, ordinals ON
        response_format="base64",  # unused — _compose_sync returns bytes
    )
    png_bytes, _w, _h = await asyncio.to_thread(
        _compose_sync, req, decoded, "contain"
    )
    return png_bytes
