"""Frame finder — pure numpy anchored per-cell frame detection (SHARED image domain).

Promoted from `services/remix/frame_finder.py` (2026-07-13) so BOTH the remix
`detect-crop-geometry` path AND the sketch `crop-base-sheet` path import ONE
canonical helper (no cross-domain import inversion). Content is otherwise the
verbatim remix implementation + one additive param (`sort_output`, below).

ANCHORED per-cell frame detection: for EACH expected cell, snap the cell's
content box on the sheet by locking onto the cell's OWN black border stroke
inside a SMALL window around its expected (scaled) position. Output = one box per
cell (inner-of-stroke, px on the image), in `expected_boxes` order — NOT yet
assigned a number.

`sort_output` (default True — remix parity): when True the output is row-major
sorted (y then x) for a stable, human-readable candidate list (remix Step-2 Gemini
re-assigns the number, so remix does NOT rely on positional order). Sketch
`crop-base-sheet` passes `sort_output=False` so `out[i]` stays paired with
`expected_boxes[i]` == `entities[i]` (positional reading-order pairing, no AI) —
re-sorting would break that invariant when a real snap shifts a box across a
row/column boundary.

Why ANCHORED (replaces the old UNANCHORED fill-holes topology, 2026-06-30):
the crop-sheet layout now packs cells with an 8px horizontal content gap and a
4px cell stroke drawn OUTSIDE each art rect — so the right stroke of cell A
(`[A_r, A_r+4)`) TOUCHES the left stroke of cell B (`[A_r+4, A_r+8)`): zero bright
gutter between columns. Global topology (`binary_fill_holes` + `label`) then
merges a whole row into ONE blob (width ≈ N× a cell) → size-sanity rejects it →
ZERO frames → every crop falls back to a static box. Per-cell anchoring sidesteps
this entirely: each cell is snapped in its OWN bounded window, so a neighbour's
touching stroke can never merge it. (Vertical separation is fine — the 64px
inter-row gutter keeps rows apart — but horizontal merging alone kills topology.)

Why touching strokes don't corrupt a per-cell snap: at cell A's right edge the
two strokes form one continuous dark run `[A_r, A_r+gap)`. `_inner_edge` returns
the ART-facing boundary of that run = `run_lo = A_r` (the neighbour's stroke only
extends the run OUTWARD, away from A's art) → A's right edge is unshifted. To make
this scale-INDEPENDENT — the sheet may be DOWNSCALED (Gemini's ~2K cap can
shrink a wide composed sheet, so the gap is `GAP_X × sx` px, possibly below a fixed
pad) — each cell's window is clamped per side to the MIDPOINT toward its nearest
neighbour's art edge (`_anchored_window`, computed from `expected_boxes`): the
window reaches its OWN touching stroke but never the neighbour's stroke/interior,
at ANY scale. `ANCHOR_OUTER_PAD_PX` is then only the cap for edge cells / wide
gutters. The top-gutter ordinal badge is likewise ignored: it is narrow (~10% of a
row's width → below EDGE_MIN_FILL) while the top stroke is full-width (~1.0) →
`_inner_edge` locks onto the stroke, never the badge.

Pipeline (per `detect_frames_anchored`, one pass over `expected_boxes`):
  ❶ composite RGBA over the gutter colour (white) → RGB  (transparent→black would
                                                          break "gutter is bright")
  ❷ gray = max(R,G,B) per pixel (black stroke → dark regardless of gutter colour)
  ❸ dark = gray < DARK_THRESH  (bool, FULL-RES — no downsample; an 8px gutter
                                cannot survive block-OR downsampling)
  ❹ per cell: outer bbox = expected box padded by ANCHOR_OUTER_PAD_PX (≤ GAP_X),
     clamped to the image; `_inner_of_stroke` snaps the 4 edges onto the cell's
     OWN stroke. Snap-fail / degenerate → fall back to the expected box (graceful,
     never-worse). NO size-sanity reject — an off-size real detection is still
     better than a wrong static box (this is the whole point of the change).
  ❺ (sort_output) row-major sort (y then x) for a stable candidate order.

`badge_bg` (reserved, default None): a future per-cell badge-landmark refine of
the top-left anchor (phase-04, DEFERRED). Production bakes a BLACK badge bg = the
same colour as the stroke, so a colour-match landmark is currently inert; the
geometry-scaled expected top-left is sufficient because the AI swap is required
to preserve the grid layout. The param is threaded + logged so phase-04 is a
clean drop-in; it has no effect today.

Helpers `to_brightness` / `_inner_edge` / `_inner_of_stroke` descend from the
deleted `border_detect.py` (this is the single owner). PII discipline: never log
image bytes; only counts / dims / box previews at DEBUG.
"""

from __future__ import annotations

import dataclasses
import logging
from io import BytesIO

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

__all__ = [
    "FrameBox",
    "DARK_THRESH",
    "EDGE_MIN_FILL",
    "ANCHOR_OUTER_PAD_PX",
    "MIN_BOX_PX",
    "GUTTER_COLOR",
    "to_brightness",
    "detect_frames_anchored",
]


# ─── Config (mapping constants — tune with real fixtures) ───────────────────

GUTTER_COLOR: tuple[int, int, int] = (255, 255, 255)  # composite RGBA over white
DARK_THRESH: int = 110  # max(R,G,B) < threshold ⇒ "dark" (stroke) pixel
EDGE_MIN_FILL: float = 0.60  # ≥60% of the edge length is a continuous dark line
# Max pad around each cell's expected box before snapping. The per-side pad is
# further clamped to the MIDPOINT toward the nearest neighbour's art edge
# (`_anchored_window`) — THAT clamp is what makes merge-safety scale-independent, so
# this constant is only the cap for edge cells / wide gutters, not the safety bound.
ANCHOR_OUTER_PAD_PX: int = 6
MIN_BOX_PX: int = 8  # snapped box smaller than this on a side ⇒ degenerate → fall back


# ─── Box ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class FrameBox:
    """A detected cell box in image pixel coords (inner-of-stroke)."""

    x: int
    y: int
    w: int
    h: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


# ─── Brightness ─────────────────────────────────────────────────────────────


def to_brightness(rgb: np.ndarray) -> np.ndarray:
    """`max(R,G,B)` per pixel → (H,W) uint8 brightness.

    Channel-max (HSV value) makes the black stroke read dark (max≈0) regardless
    of how saturated/bright the gutter colour is (a magenta gutter `#FF00FF` has
    max=255 → bright). A pixel is "dark" only when ALL channels are low — exactly
    a black border stroke. Moved verbatim from `border_detect.to_brightness`.
    """
    if rgb.ndim == 2:
        return rgb
    return rgb[..., :3].max(axis=2)


# ─── Load + composite ───────────────────────────────────────────────────────


def _load_rgb(sheet: "bytes | Image.Image | np.ndarray") -> np.ndarray:
    """Decode `sheet` → (H,W,3) uint8 RGB, compositing any alpha over GUTTER_COLOR.

    Transparent pixels MUST become the bright gutter colour, not black — otherwise
    `convert('RGB')` turns them dark and the "gutter is bright, stroke is dark"
    assumption breaks. Accepts raw PNG/JPEG bytes, a PIL image, or a pre-decoded
    numpy array (RGB or RGBA) for offline tests.
    """
    if isinstance(sheet, np.ndarray):
        arr = sheet
        if arr.ndim == 2:
            return np.stack([arr] * 3, axis=-1).astype(np.uint8)
        if arr.shape[2] == 3:
            return arr[..., :3].astype(np.uint8)
        img = Image.fromarray(arr.astype(np.uint8), mode="RGBA")
    elif isinstance(sheet, Image.Image):
        img = sheet
    else:
        img = Image.open(BytesIO(sheet))
        img.load()

    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, GUTTER_COLOR)
        bg.paste(rgba, mask=rgba.split()[-1])
        out = np.asarray(bg)
        if rgba is not img:
            rgba.close()
        return out
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    return np.asarray(rgb)


# ─── Inner-of-stroke trim ───────────────────────────────────────────────────


def _inner_edge(fills: np.ndarray, lo: int, art_after: bool) -> int | None:
    """Inner (art-facing) edge of the stroke band from a per-line fill profile.

    The stroke is the strongest dark line (full edge length → fill≈1): lock onto
    the peak, take the contiguous run ≥ EDGE_MIN_FILL around it (a short ordinal
    badge ~10% fill cannot qualify), then return the boundary on the art side:
      - art_after=True  (left/top edge): art is at higher indices → run_hi + 1.
      - art_after=False (right/bottom edge): art is at lower indices → run_lo.
    Returns the inner coord, or None when no line clears the gate. Moved from
    `border_detect._inner_edge`.
    """
    if fills.size == 0:
        return None
    peak = int(np.argmax(fills))
    if float(fills[peak]) < EDGE_MIN_FILL:
        return None
    a = peak
    while a - 1 >= 0 and fills[a - 1] >= EDGE_MIN_FILL:
        a -= 1
    b = peak
    while b + 1 < fills.size and fills[b + 1] >= EDGE_MIN_FILL:
        b += 1
    return (lo + b + 1) if art_after else (lo + a)


def _scan_band(dark: np.ndarray, axis: int) -> np.ndarray:
    """Mean dark fraction along `axis` → per-line fill profile."""
    return dark.mean(axis=axis)


def _inner_of_stroke(
    dark: np.ndarray, x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int, int, int] | None:
    """Trim a coarse box [x0,y0,x1,y1) down to the art box INSIDE the stroke.

    For each side scan an inward band for the stroke's strongest dark line and take
    its art-facing boundary. The band reaches ~18% of the cell short edge (≥12px) —
    enough to clear the stroke run. A side whose stroke is missing (gate fail) keeps
    the coarse edge (graceful). Returns `(x, y, w, h)` or None when the result is
    degenerate.
    """
    bw, bh = x1 - x0, y1 - y0
    if bw < 4 or bh < 4:
        return None
    band_v = max(12, int(0.18 * bh))  # rows scanned for top/bottom strokes
    band_h = max(12, int(0.18 * bw))  # cols scanned for left/right strokes
    band_v = min(band_v, bh // 2)
    band_h = min(band_h, bw // 2)

    # Left: columns [x0, x0+band_h), dark fraction over rows [y0,y1) per column.
    left = _inner_edge(_scan_band(dark[y0:y1, x0 : x0 + band_h], 0), x0, True)
    # Right: columns [x1-band_h, x1), inner = run start (art at lower cols).
    right = _inner_edge(_scan_band(dark[y0:y1, x1 - band_h : x1], 0), x1 - band_h, False)
    # Top: rows [y0, y0+band_v).
    top = _inner_edge(_scan_band(dark[y0 : y0 + band_v, x0:x1], 1), y0, True)
    # Bottom: rows [y1-band_v, y1), inner = run start.
    bottom = _inner_edge(_scan_band(dark[y1 - band_v : y1, x0:x1], 1), y1 - band_v, False)

    nx0 = left if left is not None else x0
    nx1 = right if right is not None else x1
    ny0 = top if top is not None else y0
    ny1 = bottom if bottom is not None else y1
    if nx1 - nx0 < 2 or ny1 - ny0 < 2:
        return None
    return (nx0, ny0, nx1 - nx0, ny1 - ny0)


# ─── Main: detect_frames_anchored ────────────────────────────────────────────


def _anchored_window(
    idx: int,
    cells: list[tuple[int, int, int, int]],
    w_img: int,
    h_img: int,
) -> tuple[int, int, int, int]:
    """Outer bbox around cell `idx`, padded by ANCHOR_OUTER_PAD_PX but clamped on
    each side to the MIDPOINT toward the nearest neighbouring cell's art edge.

    The midpoint clamp is what makes merge-safety scale-INDEPENDENT: two touching
    strokes meet at the gap midpoint, so a window that stops at the midpoint always
    contains this cell's OWN stroke and never the neighbour's — at any sheet
    scale (the pad alone, a fixed px, would over-reach on a downscaled sheet where
    the gap is `GAP_X × sx`). Neighbours are taken from `cells` (all clamped
    expected boxes); only art rects that overlap on the perpendicular axis count
    (a diagonal cell can't merge across a corner).
    """
    ax, ay, ew, eh = cells[idx]
    a_r, a_b = ax + ew, ay + eh
    lp = tp = rp = bp = ANCHOR_OUTER_PAD_PX
    for j, (jx, jy, jw, jh) in enumerate(cells):
        if j == idx:
            continue
        jr, jb = jx + jw, jy + jh
        if ay < jb and jy < a_b:  # vertical overlap → potential left/right neighbour
            if jx >= a_r:
                rp = min(rp, (jx - a_r) // 2)
            elif jr <= ax:
                lp = min(lp, (ax - jr) // 2)
        if ax < jr and jx < a_r:  # horizontal overlap → potential top/bottom neighbour
            if jy >= a_b:
                bp = min(bp, (jy - a_b) // 2)
            elif jb <= ay:
                tp = min(tp, (ay - jb) // 2)
    x0 = max(0, ax - lp)
    y0 = max(0, ay - tp)
    x1 = min(w_img, a_r + rp)
    y1 = min(h_img, a_b + bp)
    return x0, y0, x1, y1


def detect_frames_anchored(
    sheet: "bytes | Image.Image | np.ndarray",
    expected_boxes: "list[tuple[float, float, float, float]]",
    *,
    badge_bg: "tuple[int, int, int] | None" = None,
    sort_output: bool = True,
) -> list[FrameBox]:
    """Snap one content box per expected cell (ANCHORED) → FrameBoxes.

    `expected_boxes` = `[(x·sx, y·sy, w·sx, h·sy), …]` in input order — the
    geometry-scaled position+size of each cell on the sheet, used as the per-cell
    ANCHOR (not as a reject prior). Returns exactly one FrameBox per expected box
    (snapped inner-of-stroke, or the clamped expected box on snap-fail). Never
    raises on a per-cell miss — the caller statically cuts any cell whose box looks
    off, so this is always never-worse.

    `sort_output` (default True): row-major sort (y then x) for a readable
    candidate list — remix Step-2 assigns the number by content, NOT by this
    order. Pass `sort_output=False` (sketch crop-base-sheet) to KEEP the output in
    `expected_boxes` order so `out[i]` stays paired with `entities[i]` (positional
    reading-order pairing) — a real snap can shift a box across a row/column
    boundary, so re-sorting would desync the pairing.

    `badge_bg`: reserved phase-04 badge-landmark hook (currently inert — see the
    module docstring). When None (production default) the anchor is the
    geometry-scaled top-left.
    """
    rgb = _load_rgb(sheet)
    h_img, w_img = rgb.shape[:2]
    dark = to_brightness(rgb) < DARK_THRESH  # full-res dark mask (no downsample)

    # Pass 1: round + clamp every expected box to int px. Needed up-front so each
    # cell's window can be clamped against ALL its neighbours (`_anchored_window`).
    cells: list[tuple[int, int, int, int]] = []
    for (ex_f, ey_f, ew_f, eh_f) in expected_boxes:
        ax = max(0, min(int(round(ex_f)), w_img - 1))
        ay = max(0, min(int(round(ey_f)), h_img - 1))
        ew = max(1, int(round(ew_f)))
        eh = max(1, int(round(eh_f)))
        cells.append((ax, ay, ew, eh))

    # badge-landmark anchor refine — DEFERRED (phase-04). badge_bg is None in
    # production; the geometry top-left stands. See module docstring.

    # Pass 2: snap each cell inside its neighbour-clamped window.
    out: list[FrameBox] = []
    snapped = 0
    for idx, (ax, ay, ew, eh) in enumerate(cells):
        x0, y0, x1, y1 = _anchored_window(idx, cells, w_img, h_img)

        # Fallback = the expected box clamped to the image (never-worse).
        fb_w = max(1, min(ew, w_img - ax))
        fb_h = max(1, min(eh, h_img - ay))
        fallback = (ax, ay, fb_w, fb_h)

        if x1 - x0 < 4 or y1 - y0 < 4:
            box = fallback
        else:
            trimmed = _inner_of_stroke(dark, x0, y0, x1, y1)
            if trimmed is None:
                box = fallback
            else:
                tx, ty, tw, th = trimmed
                tx1, ty1 = tx + tw, ty + th
                # Per-edge graceful fallback: a side whose stroke was NOT found
                # keeps the coarse window edge (== x0/y0/x1/y1). Revert any such
                # side to the EXPECTED art edge instead — never the padded/clamped
                # window edge — so a snap-miss is the static box, not static + the
                # outer pad bled into the gutter. (A real snap always lands strictly
                # inside the window, so edge==window ⟺ that side missed.)
                nx0 = ax if tx == x0 else tx
                ny0 = ay if ty == y0 else ty
                nx1 = (ax + ew) if tx1 == x1 else tx1
                ny1 = (ay + eh) if ty1 == y1 else ty1
                if nx1 - nx0 < MIN_BOX_PX or ny1 - ny0 < MIN_BOX_PX:
                    box = fallback
                else:
                    box = (nx0, ny0, nx1 - nx0, ny1 - ny0)
                    if box != fallback:
                        snapped += 1
        out.append(FrameBox(*box))

    # Row-major sort (y then x) for a stable, human-readable candidate order —
    # unless the caller needs the input-order pairing preserved (sketch crop).
    if sort_output:
        out.sort(key=lambda b: (b.y, b.x))

    logger.debug(
        "frame_finder_anchored img=%dx%d cells=%d snapped=%d badge=%s sort=%s",
        w_img, h_img, len(out), snapped, badge_bg is not None, sort_output,
    )
    return out
