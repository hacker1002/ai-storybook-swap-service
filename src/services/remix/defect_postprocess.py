"""Shared defect post-processing — Gemini box (0-1000) → px circle + filter/cap.

DRY engine for BOTH detect-swap-defects (06, sprite plane) and detect-mix-defects
(07, mix plane). The box→circle math, drop-invalid, focus/severity filter,
severity·confidence sort, and `max_defects` cap are IDENTICAL across the two
planes — ONLY the defect MODEL class + the valid category set differ. Both are
injected (`defect_factory` + `categories`), so each core keeps its own typed
`SwapDefect` (a different `category` Literal) while sharing one engine.

Pure + deterministic — no I/O, no Pillow, no AI. PII: never logs box payloads.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

__all__ = ["SEVERITY_RANK", "DefectFactory", "coerce_box", "map_defects_to_circles"]

SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# Builds ONE plane-specific defect object from the computed scalar fields. The
# returned object MUST expose `.severity` (Optional[str]) and `.confidence`
# (Optional[float]) so the engine can sort by them after construction.
DefectFactory = Callable[..., Any]


def coerce_box(raw_box: Any) -> Optional[tuple[int, int, int, int]]:
    """Validate a Gemini `[ymin,xmin,ymax,xmax]` box (0-1000, positive area).

    Returns the 4-int tuple or None (drop). Booleans are rejected (Python bool is
    an int subclass — a stray `true` must not pass as 1).
    """
    if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
        return None
    vals: list[int] = []
    for v in raw_box:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        vals.append(int(round(v)))
    ymin, xmin, ymax, xmax = vals
    if any(c < 0 or c > 1000 for c in vals):
        return None
    if ymax <= ymin or xmax <= xmin:  # degenerate / zero-area
        return None
    return ymin, xmin, ymax, xmax


def map_defects_to_circles(
    raw_defects: list[dict],
    w_s: int,
    h_s: int,
    *,
    defect_factory: DefectFactory,
    categories,
    focus_objects: Optional[list[str]] = None,
    severity_threshold: Optional[str] = None,
    max_defects: int = 30,
    max_message_len: int = 500,
) -> tuple[list, int, bool]:
    """Convert raw Gemini defects → plane-specific defect objects (px basis).

    Steps (parity 06):
      1. drop defect when box ∉ [0,1000] / wrong shape / zero-area, OR `category`
         is present but not in `categories`;
      2. box (0-1000) → px: `x=xmin/1000·W_s`, `y=ymin/1000·H_s`, `w/h` likewise;
         `center=(x+w/2, y+h/2)`, `radius=round(0.5·hypot(w,h))`, `box` rounded;
      3. filter `focus_objects` (a defect with NO object_key is kept; only one
         tagged to a DIFFERENT object is dropped);
      4. filter `severity ≥ threshold` (a defect with no severity = 'low');
      5. sort severity desc → confidence desc; cap `max_defects` → `truncated`.

    Returns `(defects, raw_count, truncated)`; `raw_count = len(raw_defects)`
    (observability — Gemini's count BEFORE drop/filter/cap).
    """
    raw_count = len(raw_defects)
    focus_set = set(focus_objects) if focus_objects else None
    threshold_rank = SEVERITY_RANK.get(severity_threshold or "low", 0)

    mapped: list = []
    for d in raw_defects:
        box = coerce_box(d.get("box"))
        if box is None:
            continue
        category = d.get("category")
        if category is not None and category not in categories:
            continue  # invalid category → drop whole defect (spec)

        ymin, xmin, ymax, xmax = box
        x = xmin / 1000.0 * w_s
        y = ymin / 1000.0 * h_s
        w = (xmax - xmin) / 1000.0 * w_s
        h = (ymax - ymin) / 1000.0 * h_s

        severity = d.get("severity")
        if severity not in ("low", "medium", "high"):
            severity = None
        sev_rank = SEVERITY_RANK.get(severity or "low", 0)

        object_key = d.get("object_key")
        if not isinstance(object_key, str):
            object_key = None
        # focus filter: drop ONLY a defect tagged to a non-focus object.
        if focus_set is not None and object_key is not None and object_key not in focus_set:
            continue
        # severity filter.
        if sev_rank < threshold_rank:
            continue

        conf = d.get("confidence")
        confidence = (
            max(0.0, min(1.0, float(conf)))
            if isinstance(conf, (int, float)) and not isinstance(conf, bool)
            else None
        )
        cell = d.get("cell")
        if isinstance(cell, bool) or not isinstance(cell, int):
            cell = None
        message = d.get("message")
        if isinstance(message, str):
            message = message.strip()[:max_message_len] or None
        else:
            message = None

        mapped.append(
            defect_factory(
                center_x=round(x + w / 2),
                center_y=round(y + h / 2),
                radius=round(0.5 * math.hypot(w, h)),
                box_x=round(x),
                box_y=round(y),
                box_w=round(w),
                box_h=round(h),
                category=category,
                severity=severity,
                message=message,
                confidence=confidence,
                cell=cell,
                object_key=object_key,
            )
        )

    # sort: severity desc (high→low) then confidence desc.
    mapped.sort(
        key=lambda dd: (
            -SEVERITY_RANK.get(dd.severity or "low", 0),
            -(dd.confidence if dd.confidence is not None else 0.0),
        )
    )
    truncated = len(mapped) > max_defects
    if truncated:
        mapped = mapped[:max_defects]
    return mapped, raw_count, truncated
