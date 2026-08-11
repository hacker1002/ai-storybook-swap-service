"""Single source of truth for the `crop_manifest` prompt variable.

Both the request validators (mix/sprite `_check_business_limits` — byte cap) and
the core builders (`_build_variables` — prompt payload) call
`build_crop_manifest()`. Sharing one builder guarantees the byte size the cap
measures is EXACTLY the payload pumped into Gemini (no shape/byte drift).

Neutral third module (depends only on the lightweight `Crop` model) to avoid the
model↔core import cycle: core already imports the model, so the helper cannot
live in either without risking a circular import.

Manifest entry shape (spec `04-swap-mix-crop-sheet.md`):
    {"number": i+1, "geometry": {"x","y","w","h"}, **(annotation minus reserved)}

`number` matches the composer's baked ordinal badge — the badge is `index+1` for
EVERY cell (including fetch-skipped ones), so the manifest must use the same
original `req.crops` position. Reserved keys (`number`/`geometry`) inside an
annotation are silently dropped (the core-injected values win) so a caller can
never break the badge↔manifest number alignment.
"""

from __future__ import annotations

from src.models.requests.build_crop_sheet import Crop

# Keys the caller may NOT override via annotation — core owns them.
_RESERVED = ("number", "geometry")


def build_crop_manifest(crops: list[Crop]) -> list[dict]:
    """Project crops → enriched manifest entries (number + geometry + annotation)."""
    out: list[dict] = []
    for i, c in enumerate(crops):
        entry: dict = {
            "number": i + 1,
            "geometry": {
                "x": c.geometry.x,
                "y": c.geometry.y,
                "w": c.geometry.w,
                "h": c.geometry.h,
            },
        }
        if c.annotation:
            for k, v in c.annotation.items():
                if k not in _RESERVED:  # reserved → keep core-injected value
                    entry[k] = v
        out.append(entry)
    return out
