"""Fit image payloads into the Gemini 20MB inline-base64 budget.

Generic, reusable for any endpoint that sends one or more images inline to
Gemini. Per-side raw budget is 10MB (sheet + 10MB refs); a final hard-guard
on the combined base64 payload pulls everything down to <20MB if needed.

All public functions are async — Pillow C-extension decode/encode is wrapped
in `asyncio.to_thread` to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
from io import BytesIO

from PIL import Image, UnidentifiedImageError

# Side-effect: image_ops sets `Image.MAX_IMAGE_PIXELS` + `LOAD_TRUNCATED_IMAGES`.
from src.services import image_ops  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = [
    "GEMINI_INLINE_LIMIT_BYTES",
    "MAX_COMPOSED_SHEET_BYTES",
    "MAX_REFERENCE_IMAGE_BYTES",
    "MAX_HUMAN_REFS_TOTAL_BYTES",
    "MAX_HUMAN_REFS",
    "IDENTITY_HINT_MAX_EDGE",
    "MAX_IDENTITY_HINT_BYTES",
    # Mix multi-target caps + helpers (spec 04 — ⚡rev6 variant-sheet input).
    "MAX_SWAP_TARGETS",
    "MAX_MIX_SHEET_BYTES",
    "MAX_VARIANT_SHEET_BYTES",
    "VARIANT_CELL_EDGE",
    "VARIANT_SHEET_COLS",
    # Gemini technical input-image ceiling (used by sprite model 03).
    "MAX_GEMINI_INPUT_IMAGES",
    # Sprite-sheet caps (spec 03).
    "MAX_SWAP_OBJECTS",
    "MAX_SPRITE_SHEET_BYTES",
    "HUMAN_REF_MAX_EDGE",
    "HUMAN_POOL_BYTES",
    "MIN_HUMAN_REF_BYTES",
    "BudgetExceededError",
    "compute_base64_size",
    "fit_to_budget",
    "fit_group_to_budget",
    "fit_identity_hint",
    "fit_human_refs_pool",
    "enforce_total_base64_budget",
    "enforce_variant_base64_budget",
]

# Spec budget (ai-storybook-design/api/remix/04-swap-mix-crop-sheet.md
# §Gemini Payload Budget, updated 2026-05-23 for multi-image disambiguation).
GEMINI_INLINE_LIMIT_BYTES = 20 * 1024 * 1024
# Primary images: per-side raw budget lowered to make room for ≤5 aux images.
MAX_COMPOSED_SHEET_BYTES = 8 * 1024 * 1024   # 10 → 8 (primary sheet)
MAX_REFERENCE_IMAGE_BYTES = 6 * 1024 * 1024  # 10 → 6 (primary variant visual-swap ref)
MAX_HUMAN_REFS_TOTAL_BYTES = 10 * 1024 * 1024
MAX_HUMAN_REFS = 4
# Identity-hint images only need enough resolution to localise/identify a
# figure — pixel fidelity is irrelevant. Used by enhance-annotation + the
# shared `_fetch_and_shrink_hint` seam.
IDENTITY_HINT_MAX_EDGE = 1024              # px — longest edge before encode
MAX_IDENTITY_HINT_BYTES = 2 * 1024 * 1024  # raw cap per identity-hint image

# ── Mix multi-target caps (spec 04 §Gemini Payload Budget — ⚡rev6) ──────────
# rev6 variant-sheet input: payload is a FIXED 3 images (crop sheet + old-variant
# sheet + new-variant sheet) regardless of N → the shared reference pool +
# per-image identity-hint machinery is gone; simple per-sheet caps remain.
# Worst-case raw = 6 + 4 + 4 = 14MB → base64 ~18.7MB + prompt < 20MB — always
# inside the hard cap; `enforce_variant_base64_budget` is a safety net only.
MAX_SWAP_TARGETS = 10  # ⚡rev6 5→10 — above Gemini's ≤5 identity sweet spot; monitor drift
MAX_MIX_SHEET_BYTES = 6 * 1024 * 1024  # crop sheet cap (layout-critical) — spec 04 = 6MB
MAX_VARIANT_SHEET_BYTES = 4 * 1024 * 1024  # ⚡rev6 — cap PER variant sheet (old / new), raw
VARIANT_CELL_EDGE = 768  # ⚡rev6 — square slot edge px (fit-contain → longest edge ≤768)
VARIANT_SHEET_COLS = 5  # ⚡rev6 — grid: cols=min(N,5), rows=ceil(N/cols)

# Gemini hard technical input-image ceiling. The mix endpoint no longer needs it
# (fixed 3 images ≪ 14) but the sprite endpoint (03) still validates `1 + M ≤ 14`.
MAX_GEMINI_INPUT_IMAGES = 14

# ── Sprite-sheet per-object per-trait caps (spec 03 §Gemini Payload Budget) ──
# The sprite endpoint sends `1 sheet + M human` images (M ≤ 5). Lighter than 04
# (no per-cell locator). Human refs share a pool (per-human = pool // M). The
# Gemini input-image ceiling (`1 + M ≤ 14`) reuses MAX_GEMINI_INPUT_IMAGES above.
MAX_SWAP_OBJECTS = 5  # cap distinct human identities (= objects) per sheet
MAX_SPRITE_SHEET_BYTES = 6 * 1024 * 1024  # composed sprite sheet cap — spec 03 = 6MB
HUMAN_REF_MAX_EDGE = 1536  # px — pre-shrink longest edge of each human ref
HUMAN_POOL_BYTES = 8 * 1024 * 1024  # raw — SHARED across M humans; per-human = pool//M
MIN_HUMAN_REF_BYTES = 1 * 1024 * 1024  # per-human floor (except final hard-guard)

_MAX_FIT_ITERATIONS = 5
_JPEG_QUALITY = 85
_HARD_GUARD_BUFFER = 0.9  # leave 10% headroom under the absolute Gemini cap


class BudgetExceededError(Exception):
    """Raised when fit/hard-guard cannot bring payload under the budget.

    Caller (service core) maps this to `RemixDomainError(500, INTERNAL_ERROR)`
    or whichever envelope the endpoint uses.
    """


def compute_base64_size(n: int) -> int:
    """Exact base64-encoded byte size for an n-byte buffer (no I/O)."""
    if n <= 0:
        return 0
    return 4 * ((n + 2) // 3)


# ---------------------------------------------------------------------------
# Sync Pillow helpers (caller wraps in `asyncio.to_thread`)
# ---------------------------------------------------------------------------


def _decode_jpeg_q85(data: bytes) -> bytes:
    """Re-encode as JPEG q=85. Strips alpha (sheet gutter is opaque white —
    Gemini doesn't care about alpha for layout hints)."""
    with Image.open(BytesIO(data)) as src:
        src.load()
        rgb = src.convert("RGB") if src.mode != "RGB" else src
        try:
            buf = BytesIO()
            rgb.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            return buf.getvalue()
        finally:
            if rgb is not src:
                rgb.close()


def _decode_resize_jpeg(data: bytes, scale: float) -> bytes:
    """Resize by `scale` (dimensions) + re-encode JPEG q=85. Scale is clamped
    to (0.0, 1.0]; a no-op scale=1.0 still re-encodes (caller decides)."""
    scale = max(0.05, min(1.0, scale))
    with Image.open(BytesIO(data)) as src:
        src.load()
        w, h = src.size
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        rgb = src.convert("RGB") if src.mode != "RGB" else src
        try:
            resized = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
            buf = BytesIO()
            try:
                resized.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            finally:
                resized.close()
            return buf.getvalue()
        finally:
            if rgb is not src:
                rgb.close()


def _decode_resize_longest_edge(data: bytes, max_edge: int) -> bytes:
    """Downscale so the longest edge ≤ `max_edge` (LANCZOS) + re-encode JPEG
    q=85. No-op resize when already within bound (still re-encodes to JPEG —
    strips alpha + normalizes format for the identity-hint slot)."""
    with Image.open(BytesIO(data)) as src:
        src.load()
        w, h = src.size
        rgb = src.convert("RGB") if src.mode != "RGB" else src
        try:
            longest = max(w, h)
            if longest > max_edge:
                scale = max_edge / longest
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                work = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
            else:
                work = rgb
            try:
                buf = BytesIO()
                work.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
                return buf.getvalue()
            finally:
                if work is not rgb:
                    work.close()
        finally:
            if rgb is not src:
                rgb.close()


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def fit_to_budget(
    image_bytes: bytes,
    max_bytes: int,
    *,
    allow_jpeg: bool = True,
) -> bytes:
    """Bring a single image under `max_bytes` (raw, not base64).

    Strategy: pass-through → JPEG q=85 → iterative downscale (sqrt factor).
    Raises `BudgetExceededError` if even after `_MAX_FIT_ITERATIONS` downscale
    passes the result still exceeds the budget.
    """
    if max_bytes <= 0:
        raise BudgetExceededError(f"max_bytes must be positive, got {max_bytes}")
    if len(image_bytes) <= max_bytes:
        return image_bytes

    current = image_bytes
    if allow_jpeg:
        try:
            current = await asyncio.to_thread(_decode_jpeg_q85, current)
        except (UnidentifiedImageError, OSError) as exc:
            raise BudgetExceededError(f"decode failed during JPEG fit: {exc}") from exc
        if len(current) <= max_bytes:
            return current

    for _ in range(_MAX_FIT_ITERATIONS):
        factor = math.sqrt(max_bytes / max(1, len(current)))
        factor = max(0.1, min(0.95, factor))
        try:
            current = await asyncio.to_thread(_decode_resize_jpeg, current, factor)
        except (UnidentifiedImageError, OSError) as exc:
            raise BudgetExceededError(f"decode failed during downscale: {exc}") from exc
        if len(current) <= max_bytes:
            return current

    raise BudgetExceededError(
        f"could not fit {len(image_bytes)}B under {max_bytes}B after "
        f"{_MAX_FIT_ITERATIONS} downscale passes"
    )


async def fit_group_to_budget(
    images: list[bytes],
    max_total_bytes: int,
) -> list[bytes]:
    """Bring a group of images under a combined budget (raw, not base64).

    Single-pass: compute a uniform scale factor and downscale every image by
    that ratio. KISS — does not iterate; one pass is sufficient because we
    target sqrt(target/total) which converges quickly. Re-encodes JPEG q=85.
    Returns original list (no copy) when already under budget.
    """
    if not images:
        return images
    total = sum(len(b) for b in images)
    if total <= max_total_bytes:
        return images

    scale = math.sqrt(max_total_bytes / total) * _HARD_GUARD_BUFFER
    scale = max(0.3, min(0.95, scale))
    new_images = await asyncio.gather(
        *[asyncio.to_thread(_decode_resize_jpeg, b, scale) for b in images]
    )
    new_total = sum(len(b) for b in new_images)
    logger.debug(
        "fit_group_to_budget scale=%.3f before=%d after=%d budget=%d n=%d",
        scale, total, new_total, max_total_bytes, len(images),
    )
    return list(new_images)


async def fit_identity_hint(
    image_bytes: bytes, *, max_bytes: int = MAX_IDENTITY_HINT_BYTES
) -> bytes:
    """Pre-shrink an identity-hint image (target_base / unchanged reference).

    Longest edge ≤ `IDENTITY_HINT_MAX_EDGE` + JPEG q=85, then ensure ≤
    `max_bytes` via the standard single-image fit. Identity hints only convey
    appearance for figure localisation, so aggressive downscale is fine and
    keeps the multi-image payload inside the 20MB inline cap.
    Raises `BudgetExceededError` if decode fails or it cannot be brought under
    the per-hint cap.
    """
    try:
        shrunk = await asyncio.to_thread(
            _decode_resize_longest_edge, image_bytes, IDENTITY_HINT_MAX_EDGE
        )
    except (UnidentifiedImageError, OSError) as exc:
        raise BudgetExceededError(
            f"decode failed during identity-hint shrink: {exc}"
        ) from exc
    if len(shrunk) <= max_bytes:
        return shrunk
    return await fit_to_budget(shrunk, max_bytes)


async def fit_human_refs_pool(humans: list[bytes], m: int) -> list[bytes]:
    """Fit M human reference images into a SHARED `HUMAN_POOL_BYTES` budget.

    Shared-pool fit for sprite-sheet human refs (spec 03 §Gemini Payload
    Budget): per-human budget = `max(MIN_HUMAN_REF_BYTES, HUMAN_POOL_BYTES // m)`
    and each human is pre-shrunk to `HUMAN_REF_MAX_EDGE` longest edge before the
    per-human fit. Order preserved (caller maps index→object via single-source
    `ordered_images`). Raises `BudgetExceededError` (via `fit_to_budget`) if a
    human cannot be fit.
    """
    if not humans:
        return humans
    m = max(1, m)
    per_human_budget = max(MIN_HUMAN_REF_BYTES, HUMAN_POOL_BYTES // m)
    out: list[bytes] = []
    for h in humans:
        try:
            pre = await asyncio.to_thread(
                _decode_resize_longest_edge, h, HUMAN_REF_MAX_EDGE
            )
        except (UnidentifiedImageError, OSError) as exc:
            raise BudgetExceededError(
                f"decode failed during human-ref pre-shrink: {exc}"
            ) from exc
        fitted = pre if len(pre) <= per_human_budget else await fit_to_budget(
            pre, per_human_budget
        )
        out.append(fitted)
    logger.debug(
        "fit_human_refs_pool m=%d per_human_budget=%d before=%d after=%d",
        m, per_human_budget, sum(len(b) for b in humans), sum(len(b) for b in out),
    )
    return out


async def enforce_total_base64_budget(
    sheet_bytes: bytes,
    ref_bytes_list: list[bytes],
    prompt_str: str,
    *,
    hard_limit_bytes: int = GEMINI_INLINE_LIMIT_BYTES,
    identity_hints: list[bytes] | None = None,
) -> tuple[bytes, list[bytes], list[bytes]]:
    """Hard-guard the total base64-encoded payload size (2-tier).

    Combines `base64(sheet) + Σ base64(refs) + Σ base64(identity_hints) +
    len(prompt.utf8)`. When over the limit, shrinks in priority order:
      Tier 1 — identity hints only (least important pixels), IF the primary
               group (sheet + refs) already fits on its own.
      Tier 2 — uniform downscale of every image (primary + already-reduced
               hints) with a 10% headroom buffer.
    Returns `(sheet, refs, identity_hints)`. `identity_hints` defaults to `[]`
    (callers that send only primary images — e.g. endpoint 03 — ignore the 3rd
    element). Raises `BudgetExceededError` if the post-scale total still
    exceeds the limit.
    """
    hints: list[bytes] = list(identity_hints or [])
    prompt_bytes = len(prompt_str.encode("utf-8"))
    sheet_b64 = compute_base64_size(len(sheet_bytes))
    refs_b64 = sum(compute_base64_size(len(b)) for b in ref_bytes_list)
    hints_b64 = sum(compute_base64_size(len(b)) for b in hints)
    total_b64 = sheet_b64 + refs_b64 + hints_b64 + prompt_bytes

    # 1KB safety margin protects against the `total == hard` boundary getting
    # rejected upstream by Gemini despite being exactly at the limit.
    if total_b64 + 1024 <= hard_limit_bytes:
        return sheet_bytes, ref_bytes_list, hints

    # ── Tier 1: shrink identity hints first (only if primary already fits) ──
    primary_b64 = sheet_b64 + refs_b64 + prompt_bytes
    if hints and primary_b64 + 1024 <= hard_limit_bytes:
        avail_for_hints = hard_limit_bytes - primary_b64 - 1024
        scale = math.sqrt(avail_for_hints / max(1, hints_b64)) * _HARD_GUARD_BUFFER
        scale = max(0.2, min(0.95, scale))
        hints = list(
            await asyncio.gather(
                *[asyncio.to_thread(_decode_resize_jpeg, b, scale) for b in hints]
            )
        )
        new_total = (
            sheet_b64
            + refs_b64
            + sum(compute_base64_size(len(b)) for b in hints)
            + prompt_bytes
        )
        logger.warning(
            "enforce_total_base64_budget tier1=identity scale=%.3f before=%d after=%d hard=%d hints=%d",
            scale, total_b64, new_total, hard_limit_bytes, len(hints),
        )
        if new_total + 1024 <= hard_limit_bytes:
            return sheet_bytes, ref_bytes_list, hints

    # ── Tier 2: uniform downscale of every image ────────────────────────────
    all_imgs: list[bytes] = [sheet_bytes, *ref_bytes_list, *hints]
    raw_payload_b64 = max(1, sum(compute_base64_size(len(b)) for b in all_imgs))
    available = max(1, hard_limit_bytes - prompt_bytes)
    scale = math.sqrt(available / raw_payload_b64) * _HARD_GUARD_BUFFER
    scale = max(0.3, min(0.95, scale))

    logger.warning(
        "enforce_total_base64_budget tier2=uniform scale=%.3f hard=%d sheet=%d refs=%d hints=%d prompt=%d",
        scale, hard_limit_bytes,
        len(sheet_bytes), len(ref_bytes_list), len(hints), prompt_bytes,
    )

    scaled = await asyncio.gather(
        *[asyncio.to_thread(_decode_resize_jpeg, b, scale) for b in all_imgs]
    )
    n_refs = len(ref_bytes_list)
    new_sheet = scaled[0]
    new_refs: list[bytes] = list(scaled[1 : 1 + n_refs])
    new_hints: list[bytes] = list(scaled[1 + n_refs :])

    new_total = (
        compute_base64_size(len(new_sheet))
        + sum(compute_base64_size(len(b)) for b in new_refs)
        + sum(compute_base64_size(len(b)) for b in new_hints)
        + prompt_bytes
    )
    if new_total >= hard_limit_bytes:
        raise BudgetExceededError(
            f"post-scale base64 total {new_total} still ≥ hard limit {hard_limit_bytes}"
        )

    return new_sheet, new_refs, new_hints


async def _scale_group(images: list[bytes], scale: float) -> list[bytes]:
    """Re-encode/downscale every image in `images` by `scale` (helper for the
    mix hard-guard tiers). Returns a fresh list; empty input → empty list."""
    if not images:
        return []
    return list(
        await asyncio.gather(
            *[asyncio.to_thread(_decode_resize_jpeg, b, scale) for b in images]
        )
    )


async def enforce_variant_base64_budget(
    crop_sheet: bytes,
    old_sheet: bytes | None,
    new_sheet: bytes,
    prompt_str: str,
    *,
    hard_limit_bytes: int = GEMINI_INLINE_LIMIT_BYTES,
) -> tuple[bytes, bytes | None, bytes]:
    """Hard-guard the total ⚡rev6 mix payload (FIXED 3 images), shrinking in
    spec-04 priority order (least → most important): **old-variant sheet →
    new-variant sheet → crop sheet** (the old sheet only needs to be
    recognisable; the crop sheet is layout-critical, kept at full res longest).

    `old_sheet` is None for the N=1 degenerate case without a target_base
    (2-image payload). With the static caps (6 + 4 + 4 MB raw → ~18.7MB base64)
    this guard almost never fires — pure safety net. Returns the (possibly
    shrunk) `(crop_sheet, old_sheet, new_sheet)`. Raises `BudgetExceededError`
    if still over budget after the final tier.
    """
    prompt_bytes = len(prompt_str.encode("utf-8"))

    def _total() -> int:
        return (
            compute_base64_size(len(crop_sheet))
            + (compute_base64_size(len(old_sheet)) if old_sheet is not None else 0)
            + compute_base64_size(len(new_sheet))
            + prompt_bytes
        )

    # 1KB safety margin (same rationale as `enforce_total_base64_budget`).
    if _total() + 1024 <= hard_limit_bytes:
        return crop_sheet, old_sheet, new_sheet

    # 3 tiers, in increasing priority of preservation. Each tier applies up to
    # `_MAX_FIT_ITERATIONS` fixed-step shrink passes before falling through.
    for which in ("old", "new", "crop_sheet"):
        for _ in range(_MAX_FIT_ITERATIONS):
            if _total() + 1024 <= hard_limit_bytes:
                logger.warning(
                    "enforce_variant_base64_budget settled tier=%s total≈%d hard=%d",
                    which, _total(), hard_limit_bytes,
                )
                return crop_sheet, old_sheet, new_sheet
            if which == "old":
                if old_sheet is None:
                    break
                old_sheet = (await _scale_group([old_sheet], 0.7))[0]
            elif which == "new":
                new_sheet = (await _scale_group([new_sheet], 0.7))[0]
            else:  # crop_sheet
                crop_sheet = (await _scale_group([crop_sheet], 0.7))[0]

    if _total() + 1024 <= hard_limit_bytes:
        return crop_sheet, old_sheet, new_sheet

    raise BudgetExceededError(
        f"variant payload base64 total {_total()} still ≥ hard limit "
        f"{hard_limit_bytes} after all 3 tiers"
    )
