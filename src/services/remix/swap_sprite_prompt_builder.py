"""Prompt-variable builders for the per-object per-trait sprite-sheet swap (03).

Split out of `swap_sprite_sheet_core.py` to keep the core under 500 LOC and to
make the single-source indexing unit-testable in isolation. The highest-risk
concern is **image_guide ↔ human_image_index mismatch**: the Gemini content_parts
list is `[prompt, sheet, human_1, human_2, ..., human_M]` where humans appear in
`swap_objects[]` order (HUMAN_FETCH_ERROR is FATAL → no sparse list → contiguous
indices). The builders receive a precomputed `SpriteImagePlan` whose indices come
from the REAL ordered-images append order — they NEVER recompute positions.

Prompt shape (revised 2026-06-09 per user feedback): the per-cell plan is now a
JSON array (was prose) — one object per sheet cell spelling out exactly: which
object, which reference image (as a string token `"#2"` cross-referencing the
image map), and ONLY the traits to swap. The per-cell "keep other traits" line is
GONE — the preserve rule is stated once, declaratively, in the system prompt.
JSON gives the model a clean lookup table and removes the prose/plan
duplication that invited self-contradiction → drift.
Character-context fields (basic_info / appearance / visual_description) are NOT
surfaced — only the cell→object→reference→traits mapping the model must act on.

PII discipline: only object names (story names, not real people) + entity keys +
the caller-provided trait descriptions are surfaced INTO the prompt (the prompt is
sent to Gemini, not logged). Builders NEVER log; the core logs only counts.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from typing import Any

# Canonical trait sort order — deterministic prompt rendering.
from src.models.requests.trait_types import TRAIT_TYPES
# Shared reference contract (Phase 2 conform) — the builder returns the atomic
# `(parts, guide_text, manifest_vars)` unit; the core only fetches/fits bytes.
from src.services.reference_prompt_builder import (
    BuiltReferences,
    ReferenceRole,
    ReferenceSpec,
)


def _image_part(data: bytes, mime: str) -> dict:
    """Image-part dict shape shared across the Gemini prompt builders
    (`data:<mime>;base64,<b64>`). Local (not imported from the shared module's
    private helper) to keep this builder self-contained — same 1-line shape."""
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": f"data:{mime};base64,{b64}"}


@dataclasses.dataclass(slots=True, frozen=True)
class SpriteObjectPlan:
    """Resolved per-object swap config + its 1-based human image position.

    `human_image_index` is the position of this object's human ref in
    `ordered_images` (1 = sheet, so human positions start at 2). Set during the
    core single-source pass — never recomputed here. Character-context fields are
    deliberately absent: the prompt only needs the cell→object→target→traits map.
    """

    object_key: str
    name: str
    swap_traits: list[tuple[str, str]]  # (type, description), sorted by TRAIT_TYPES
    human_image_index: int


@dataclasses.dataclass(slots=True, frozen=True)
class SpriteImagePlan:
    """The full, ordered image plan derived from the ACTUAL fetched images.

    `objects` are in `swap_objects[]` order; the sheet is always image #1, each
    object's human ref follows at its `human_image_index`.
    """

    objects: list[SpriteObjectPlan]


def build_sprite_image_guide(plan: SpriteImagePlan) -> str:
    """Index-pinned image map mirroring content_parts order exactly.

    Tells the model which attached image # is the sheet and which is each object's
    normalized visual. Every human is present (HUMAN_FETCH_ERROR is fatal
    upstream), so indices are contiguous `2..M+1`.

    Trait DESCRIPTIONS live HERE (revised 2026-06-09), attached to the object's
    reference image — NOT inline in the per-cell plan. Rationale: the description
    (e.g. "short black hair") describes the appearance to APPLY from this reference;
    placed next to `swap` in a cell it reads ambiguously (is the ORIGINAL cell
    short-black-haired?). Anchoring it to the reference image, framed as "đặc điểm
    cần áp", removes that confusion and de-duplicates (per-object, listed once).
    The per-cell plan then carries only the trait TYPES to swap.
    """
    lines = [
        "- Ảnh #1: Sprite sheet gốc — lưới N ô, mỗi ô 1 variant; số thứ tự bake "
        "góc trên-trái mỗi ô.",
    ]
    for o in plan.objects:
        traits_str = "; ".join(f"{t} - {d}" for (t, d) in o.swap_traits)
        lines.append(
            f'- Ảnh #{o.human_image_index}: Ảnh tham chiếu nhân vật "{o.name}" '
            f"(object_key={o.object_key}), đã chuẩn hoá theo phong cách minh hoạ — "
            "nguồn đặc điểm tạo hình để vẽ cho nhân vật này (KHÔNG vẽ chính ảnh này "
            "vào output). Đặc điểm ngoại hình CẦN VẼ (diện mạo mặc định, không phải "
            f"mô tả ô gốc): {traits_str}."
        )
    return "\n".join(lines)


def build_sprite_cell_swap_plan(
    crops: list[Any], plan: SpriteImagePlan
) -> str:
    """PROMINENT per-cell swap plan as a JSON array — the most important section.

    One JSON object per sheet cell, in composer bake order (`cell` = index+1,
    matching the ordinal baked onto the sheet — OQ#10). Each entry carries:
      - `cell`            : baked ordinal (int)
      - `region`          : `[x, y, w, h]` for cut alignment
      - `object`/`variant`: join keys (opaque echo)
      - `name`            : story name (display)
      - `reference_image` : STRING token `"#<idx>"` cross-referencing the image
                            map (e.g. `"#2"`) — NOT a bare int, so the model reads
                            it as the same label used in `image_guide`
      - `swap`            : `["face", "hair", ...]` — ONLY the trait TYPES to change.
                            The target DESCRIPTION for each trait lives in the
                            image_guide (attached to the reference image), NOT here
                            — see `build_sprite_image_guide` (revised 2026-06-09)

    Cells whose `object_key` has no swap config carry `keep_original: true` (and no
    `reference_image`/`swap`) — generic fallback (the job never sends these, but
    the primitive tolerates them). The per-cell "keep other traits" instruction is
    intentionally absent — the preserve rule lives once in the system prompt.

    `crops` are the request's `SpriteSheetCrop[]` (duck-typed: `.geometry.{x,y,w,h}`,
    `.object_key`, `.variant_key`). `plan.objects` carry the resolved 1-based
    `human_image_index` + sorted traits. The two are joined by `object_key`.
    """
    by_key = {o.object_key: o for o in plan.objects}
    entries: list[dict] = []
    for i, c in enumerate(crops):
        g = c.geometry
        o = by_key.get(c.object_key)
        entry: dict = {
            "cell": i + 1,
            "region": [g.x, g.y, g.w, g.h],
            "object": c.object_key,
            "variant": c.variant_key,
            "name": o.name if o is not None else c.object_key,
        }
        if o is None:
            entry["keep_original"] = True
        else:
            entry["reference_image"] = f"#{o.human_image_index}"
            # Trait TYPES only — descriptions live in the image_guide (per-object).
            entry["swap"] = [t for (t, _d) in o.swap_traits]
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False, indent=2)


def sort_traits(traits: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sort (type, description) pairs by the canonical `TRAIT_TYPES` order."""
    return sorted(traits, key=lambda td: TRAIT_TYPES.index(td[0]))


# ── Atomic builder (Phase 2 conform) ───────────────────────────────────────
# `build_sprite_references` is the SINGLE public entry point: it OWNS the whole
# `(parts, guide_text, manifest_vars)` tuple — index assignment, trait sort, part
# order, and the per-cell plan. `SpriteObjectPlan`/`SpriteImagePlan` +
# `build_sprite_image_guide`/`build_sprite_cell_swap_plan`/`sort_traits` above
# are now INTERNAL helpers it composes (kept public for the existing unit tests).


@dataclasses.dataclass(slots=True, frozen=True)
class SpriteObjectInput:
    """One swap object as the core hands it to the builder — NO image index.

    The builder assigns `human_image_index` (single source) from this list's
    order, so the core no longer pre-computes positions. `swap_traits` may be
    unsorted — the builder canonicalises via `sort_traits` (single source).
    """

    object_key: str
    name: str
    swap_traits: list[tuple[str, str]]  # (type, description); builder sorts


def build_sprite_references(
    sheet_spec: ReferenceSpec,
    human_specs: list[ReferenceSpec],
    objects: list[SpriteObjectInput],
    crops: list[Any],
    *,
    result_spec: ReferenceSpec | None = None,
    has_result: bool = False,
) -> BuiltReferences:
    """Atomic `(parts, guide_text, manifest_vars)` for the sprite swap.

    `human_specs` + `objects` are PARALLEL (same order, same length —
    `swap_objects[]` order). The builder:
      1. assigns each object `human_image_index = i + 2` (#1 = sheet) — the ONE
         source of truth for both the image guide and the per-cell plan;
      2. emits `parts = [CROP_SHEET, *HUMAN_REF]` using each spec's `mime_type`;
      3. renders `guide_text` (`build_sprite_image_guide`) + the per-cell plan
         (`build_sprite_cell_swap_plan`) from that SAME plan → numbering can
         never drift from the parts list.

    `has_result=True` + `result_spec` (detect-swap-defects, spec 06): append the
    RESULT image as the FINAL part (index `#(M+2)`) and a matching guide line
    pinning it as the inspection target ("Mọi toạ độ box tính trên ảnh NÀY"). This
    is backward-compatible — the default (`has_result=False`) reproduces the swap
    (03) output byte-for-byte (guarded by the builder parity test).

    PURE — bytes arrive already fetched/fitted in the specs; the builder only
    base64-encodes them. Re-call with new bytes if the image set changes (the
    atomic-unit invariant of the shared contract).
    """
    plan = SpriteImagePlan(
        objects=[
            SpriteObjectPlan(
                object_key=o.object_key,
                name=o.name,
                swap_traits=sort_traits(o.swap_traits),
                human_image_index=i + 2,  # #1 = sheet, humans start at #2
            )
            for i, o in enumerate(objects)
        ]
    )
    parts: list[dict] = [_image_part(sheet_spec.image_bytes, sheet_spec.mime_type)]
    parts.extend(_image_part(s.image_bytes, s.mime_type) for s in human_specs)
    guide_text = build_sprite_image_guide(plan)

    if has_result and result_spec is not None:
        # #1 = sheet, humans = #2..#(M+1), result = #(M+2).
        result_idx = len(objects) + 2
        parts.append(_image_part(result_spec.image_bytes, result_spec.mime_type))
        guide_text += (
            f"\n- Ảnh #{result_idx}: KẾT QUẢ swap — ghép lại từ các ô ĐÃ swap, CÙNG "
            "khung/lưới/số ô như Ảnh #1 (chỉ khác NỘI DUNG trong từng ô). ĐÂY là ảnh "
            "cần soi lỗi — mọi toạ độ box tính trên ảnh NÀY."
        )

    return BuiltReferences(
        parts=parts,
        guide_text=guide_text,
        count=len(parts),
        manifest_vars={"cell_swap_plan": build_sprite_cell_swap_plan(crops, plan)},
    )
