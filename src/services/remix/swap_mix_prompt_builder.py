"""Prompt-variable builders for the multi-target mix crop-sheet swap (04).

⚡rev6 (2026-06-11) — variant-sheet input. Gemini receives a FIXED 3 images
(crop sheet + old-variant sheet + new-variant sheet; 2 when N=1 without a
target_base), so the old per-target index bookkeeping (`MixImagePlan` /
`MixTargetPlan`, content_parts positions per target) is gone. Two prompt
variables are built here:

  - `image_guide` — describes the 2-3 ACTUAL images + the cell-pairing rule
    (cell i on the old sheet ↔ cell i on the new sheet = the SAME target i) +
    the two INDEPENDENT number scales (crop-cell ordinals vs target ordinals).
  - `variant_manifest` — JSON array, one entry per target: `number` (== baked
    badge on BOTH variant sheets), `geometry` (the shared layout cell — mirror),
    `object` (variant-qualified mention, byte-identical to `crop_manifest[]
    .objects` entries via `object_mention`), `name`, and `recognition_hint`
    (the ORIGINAL `object_context.visual_description`, capped, omitted when
    empty — inherits the 2026-06-11 locator-hint fix).

Generic over object type: targets may be characters OR props (items). The
model-facing prose uses "đối tượng"/"mục tiêu" so Gemini treats all kinds
uniformly, and keeps the 2026-06-10 content-policy reframe (no "swap"/
"identity" verbs).

PII discipline: only object names (story names, not real people) + entity keys
are surfaced. No URLs / bytes / base64.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from typing import TYPE_CHECKING, Any

# Shared reference contract (Phase 2 conform) — the mix builder returns the
# atomic `(parts, guide_text, manifest_vars)` unit; the core only composes/fits
# the sheet bytes into the specs.
from src.services.reference_prompt_builder import (
    BuiltReferences,
    ReferenceRole,
    ReferenceSpec,
)

if TYPE_CHECKING:  # typing only — avoids a runtime model↔builder import cycle
    from src.models.requests.build_crop_sheet import Geometry
    from src.models.requests.swap_mix_crop_sheet import SwapTarget

__all__ = [
    "MAX_LOCATOR_HINT_CHARS",
    "object_mention",
    "build_variant_image_guide",
    "build_variant_manifest",
    "build_mix_detect_image_guide",
    "build_detect_builder_params",
    "MixCropInput",
    "build_mix_references",
]

# Manifest keys the builder OWNS — never copied from the free-form annotation
# (number/geometry are core-injected; objects is rendered first-class from the
# bundled `MixCropInput.objects`, so a stale annotation `objects` can't shadow it).
_MANIFEST_RESERVED = ("number", "geometry", "objects")


def _image_part(data: bytes, mime: str) -> dict:
    """Image-part dict shape (`data:<mime>;base64,<b64>`) — local 1-line copy to
    keep the builder self-contained (parity with the sprite builder)."""
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": f"data:{mime};base64,{b64}"}

# Per-target recognition-hint cap (variant_manifest). visual_description is
# caller data (snapshot variants allow long prose) — N≤10 targets × 400 chars
# keeps the grounding bounded while preserving the outfit/relative-age cues.
MAX_LOCATOR_HINT_CHARS = 400


def object_mention(object_key: str, variant_key: str) -> str:
    """Variant-qualified mention — the SHARED join token across the prompt.

    Byte-identical wherever an object is referenced: `variant_manifest[].object`
    AND per-cell `objects` annotation entries (jobs handler) both render through
    this helper, so the model can string-match a cell's object list against the
    target list without inference. A bare `@key` cannot distinguish two variants
    of the same object — the variant qualifier is always emitted.
    """
    variant = (variant_key or "base").strip() or "base"
    return f"@{object_key}, biến thể: {variant}"


def _crop_sheet_line(n_crops: int) -> str:
    """Ảnh #1 line — pins the ACTUAL crop-cell count (a single-crop sheet
    described as "many cells" invites hallucinated panel splits)."""
    if n_crops == 1:
        return (
            "- Ảnh #1: Crop sheet gốc — sheet này CHỈ CÓ MỘT ô crop duy nhất "
            "(đánh số 1): TOÀN BỘ sheet là một ô, KHÔNG chia nhỏ/tách panel. "
            "Khuôn mẫu bố cục / art style / tư thế / biểu cảm."
        )
    if n_crops > 1:
        return (
            f"- Ảnh #1: Crop sheet gốc gồm ĐÚNG {n_crops} ô crop "
            f"(đánh số 1..{n_crops}) — khuôn mẫu bố cục / art style / "
            "tư thế / biểu cảm."
        )
    return (
        "- Ảnh #1: Crop sheet gốc — khuôn mẫu bố cục / art style / tư thế / "
        "biểu cảm."
    )


def build_variant_image_guide(
    n_crops: int, n_targets: int, has_old_sheet: bool
) -> str:
    """⚡rev6 image guide — describes the FIXED image layout actually sent.

    `has_old_sheet=True` → 3 images (#2 = old-variant sheet, #3 = new-variant
    sheet). `has_old_sheet=False` (N=1 degenerate without target_base) → 2
    images (#2 = new-variant sheet); localisation falls back to the
    `recognition_hint` inside the target list. Image indices in the guide ALWAYS
    match the real content_parts order (deterministic).
    """
    lines = [_crop_sheet_line(n_crops)]
    cell_range = "1" if n_targets == 1 else f"1..{n_targets}"
    if has_old_sheet:
        lines.append(
            f"- Ảnh #2: BẢNG DIỆN MẠO GỐC — {n_targets} ô mục tiêu (đánh số "
            f"{cell_range}); ô số i = diện mạo GỐC của mục tiêu i — ảnh ĐỊNH VỊ "
            "(KHÔNG vẽ vào output)."
        )
        lines.append(
            f"- Ảnh #3: BẢNG DIỆN MẠO MỚI — {n_targets} ô mục tiêu, CÙNG SỐ + "
            "CÙNG VỊ TRÍ với Ảnh #2; ô số i = diện mạo MỚI CẦN VẼ cho mục tiêu i."
        )
        lines.append(
            "- Ghép cặp: ô số i (Ảnh #2) ↔ ô số i (Ảnh #3) = CÙNG MỘT mục tiêu. "
            'Định danh + đặc điểm nhận diện từng mục tiêu: xem "Danh sách mục '
            'tiêu" (khớp theo number).'
        )
    else:
        lines.append(
            f"- Ảnh #2: BẢNG DIỆN MẠO MỚI — {n_targets} ô mục tiêu (đánh số "
            f"{cell_range}); ô số i = diện mạo MỚI CẦN VẼ cho mục tiêu i."
        )
        lines.append(
            '- Định vị mục tiêu trong sheet bằng "Danh sách mục tiêu" (khớp '
            "theo number — dùng recognition_hint để nhận diện đối tượng)."
        )
    lines.append(
        "- LƯU Ý 2 thang số ĐỘC LẬP: số trên Ảnh #1 là CHỈ SỐ Ô CROP (tra "
        "crop_manifest); số trên các bảng diện mạo là CHỈ SỐ MỤC TIÊU (tra "
        '"Danh sách mục tiêu").'
    )
    return "\n".join(lines)


def build_variant_manifest(
    swap_targets: "list[SwapTarget]", layout_cells: "list[Geometry]"
) -> str:
    """⚡rev6 `variant_manifest` JSON — one entry per target, `number` matches
    the baked badge on BOTH variant sheets, `geometry` is the shared layout cell
    (mirror invariant). `object` renders through `object_mention` so it is
    byte-identical to the `crop_manifest[].objects` entries (3-way join: crop
    cell ↔ target ↔ variant-sheet cell). `recognition_hint` = the ORIGINAL
    `visual_description`, capped, omitted when empty.
    """
    if len(swap_targets) != len(layout_cells):
        raise ValueError(
            f"swap_targets count {len(swap_targets)} != layout cells "
            f"{len(layout_cells)}"
        )
    entries: list[dict] = []
    for i, t in enumerate(swap_targets):
        # `key` is the variant-qualified token `${object_key}/${variant_key}`
        # (bare key → variant 'base').
        object_key, _, variant_part = t.key.partition("/")
        variant_key = variant_part.strip() or "base"
        cell = layout_cells[i]
        entry: dict = {
            "number": i + 1,
            "geometry": {"x": cell.x, "y": cell.y, "w": cell.w, "h": cell.h},
            "object": object_mention(object_key, variant_key),
            "name": t.object_context.name or object_key,
        }
        hint = (t.object_context.visual_description or "").strip()[
            :MAX_LOCATOR_HINT_CHARS
        ]
        if hint:
            entry["recognition_hint"] = hint
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False, indent=2)


# ── detect-mix-defects (07) prompt vars ─────────────────────────────────────
# detect (07) REUSES `build_variant_manifest` + the crop_manifest renderer 1:1
# (the standard for judging WHICH region is wrong), and adds the RESULT image to
# the guide + a detect-flavoured builder_params. These two functions live HERE
# (not in the 07 core) so the whole mix prompt-variable family stays in one
# place — DRY with swap 04.


def build_mix_detect_image_guide(
    n_crops: int, n_targets: int, has_old: bool, has_result: bool = True
) -> str:
    """⚡detect (07) image guide — the FIXED 3-4 image layout of a MIX detect call.

    Mirrors `build_variant_image_guide` (Ảnh #1 crop sheet GỐC, [Ảnh # BẢNG GỐC],
    Ảnh # BẢNG MỚI) but ADDS the RESULT sheet as the LAST image (the inspection
    target — all defect boxes are measured on it) and pins the TWO INDEPENDENT
    number scales (crop-cell ordinals vs target ordinals). Image indices ALWAYS
    match the real content_parts order [orig, old?, new, result] (deterministic).
    """
    i = 1
    lines = [
        _crop_sheet_line(n_crops)
        + " Số ô = CHỈ SỐ Ô CROP (tra crop_manifest)."
    ]
    i += 1
    cell_range = "1" if n_targets == 1 else f"1..{n_targets}"
    if has_old:
        lines.append(
            f"- Ảnh #{i}: BẢNG DIỆN MẠO GỐC — {n_targets} ô mục tiêu (số {cell_range}); "
            "ô số j = diện mạo GỐC của mục tiêu j — ảnh ĐỊNH VỊ figure nào là target "
            "nào (KHÔNG vẽ vào output). Số ô = CHỈ SỐ MỤC TIÊU (tra variant_manifest)."
        )
        i += 1
    lines.append(
        f"- Ảnh #{i}: BẢNG DIỆN MẠO MỚI — {n_targets} ô mục tiêu, CÙNG SỐ + VỊ TRÍ "
        "với bảng GỐC; ô số j = diện mạo ĐÍCH đáng lẽ áp cho mục tiêu j. Số ô = "
        "CHỈ SỐ MỤC TIÊU."
        if has_old
        else f"- Ảnh #{i}: BẢNG DIỆN MẠO MỚI — {n_targets} ô mục tiêu (số {cell_range}); "
        "ô số j = diện mạo ĐÍCH đáng lẽ áp cho mục tiêu j. Số ô = CHỈ SỐ MỤC TIÊU."
    )
    i += 1
    if has_old:
        lines.append(
            "- Ghép cặp: ô số j (bảng GỐC) ↔ ô số j (bảng MỚI) = CÙNG MỘT mục tiêu j "
            "(tra variant_manifest theo number)."
        )
    else:
        lines.append(
            '- Định vị mục tiêu trong sheet bằng "Danh sách mục tiêu" / variant_manifest '
            "(khớp theo number — dùng recognition_hint để nhận diện đối tượng)."
        )
    if has_result:
        lines.append(
            f"- Ảnh #{i}: KẾT QUẢ swap (ghép lại từ các ô đã swap — CÙNG khung/lưới/số "
            "ô crop như Ảnh #1, chỉ khác NỘI DUNG trong ô) — ĐÂY là ảnh CẦN SOI LỖI. "
            "MỌI toạ độ box tính TRÊN ảnh NÀY. Số ô = CHỈ SỐ Ô CROP."
        )
    lines.append(
        "- LƯU Ý 2 thang số ĐỘC LẬP: số trên Ảnh #1 / Ảnh KẾT QUẢ = CHỈ SỐ Ô CROP "
        "(tra crop_manifest); số trên các bảng diện mạo = CHỈ SỐ MỤC TIÊU (tra "
        "variant_manifest)."
    )
    return "\n".join(lines)


def build_detect_builder_params(
    model: "str | None", temperature: "float | None"
) -> str:
    """`{%request.builder_params%}` for detect (07) — the swap params used + the
    invariants Gemini must treat as "must hold" (a violation at a region = a
    defect there). Generic text only (model/temp + mix invariants); NO human
    data embedded (PII discipline)."""
    model_str = model or "(mặc định)"
    temp_str = str(temperature) if temperature is not None else "(mặc định)"
    return (
        f"- Model image-gen đã dùng: {model_str} ; temperature: {temp_str}.\n"
        "- Swap chạy 1 call image-gen DUY NHẤT cho cả sheet, áp N diện mạo MỚI lên "
        "N figure mục tiêu.\n"
        "- BẮT BUỘC giữ (vi phạm = vùng lỗi): lưới + số ô crop (ordinal), pose + "
        "biểu cảm từng ô, art style, và MỌI đối tượng KHÔNG nằm trong danh sách mục "
        "tiêu (variant_manifest) — giữ nguyên tuyệt đối.\n"
        "- Map đúng từng mục tiêu qua cặp ô số j trên 2 bảng variant (GỐC↔MỚI), "
        "KHÔNG lẫn diện mạo giữa các mục tiêu (cross-contamination). Mỗi ô crop chỉ "
        "đổi các mục tiêu CÓ MẶT trong ô (theo crop_manifest.objects)."
    )


# ── Atomic builder (Phase 2 conform) ───────────────────────────────────────
# `build_mix_references` is the SINGLE public entry point: it OWNS the whole
# `(parts, guide_text, manifest_vars)` tuple — presence-of-role old sheet, part
# order [CROP_SHEET, OLD?, NEW], and BOTH manifests (variant + crop). The roster
# arrives bundled per-crop via `MixCropInput` (Validation S1 Q1) — no parallel
# array. `build_variant_image_guide`/`build_variant_manifest`/`object_mention`
# above are now INTERNAL helpers it composes (kept public for the existing tests).
# `crop_manifest` (crop_manifest.py `build_crop_manifest`) stays ROSTER-FREE —
# it serves ONLY the request validator's byte cap; the builder renders its own
# crop_manifest from `MixCropInput` so `objects` is a first-class field.


@dataclasses.dataclass(slots=True, frozen=True)
class MixCropInput:
    """One crop bundled with its per-cell object roster (Validation S1 Q1).

    `crop` is duck-typed (`.geometry.{x,y,w,h}`, `.annotation`) — the request
    `Crop`. `objects` are the variant-qualified mentions (deduped, sorted) for
    this cell, rendered FIRST-CLASS into `crop_manifest[].objects` — never read
    back out of the free-form `annotation`, so the roster can't drift.
    """

    crop: Any
    objects: list[str]


def _render_crop_manifest(crops_with_roster: list[MixCropInput]) -> str:
    """Render `crop_manifest` JSON directly from the bundled inputs.

    Entry shape (parity with `crop_manifest.py`): `{number, geometry,
    **(annotation minus reserved), objects?}`. `number` = baked ordinal (i+1 for
    EVERY cell). `objects` is emitted LAST and only when non-empty (byte-parity
    with the prior jobs-handler path that appended `objects` after the map keys).
    """
    entries: list[dict] = []
    for i, mci in enumerate(crops_with_roster):
        g = mci.crop.geometry
        entry: dict = {
            "number": i + 1,
            "geometry": {"x": g.x, "y": g.y, "w": g.w, "h": g.h},
        }
        ann = getattr(mci.crop, "annotation", None)
        if isinstance(ann, dict):
            for k, v in ann.items():
                if k not in _MANIFEST_RESERVED:
                    entry[k] = v
        if mci.objects:
            entry["objects"] = list(mci.objects)
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False, indent=2)


def build_mix_references(
    crop_sheet_spec: ReferenceSpec,
    new_variant_spec: ReferenceSpec,
    old_variant_spec: ReferenceSpec | None,
    swap_targets: "list[SwapTarget]",
    layout_cells: "list[Geometry]",
    crops_with_roster: list[MixCropInput],
) -> BuiltReferences:
    """Atomic `(parts, guide_text, manifest_vars)` for the mix swap.

    Old sheet is inferred by PRESENCE-OF-ROLE: `old_variant_spec is not None` →
    3 images `[CROP_SHEET, OLD_VARIANT_SHEET, NEW_VARIANT_SHEET]`; None (N=1
    degenerate) → 2 images `[CROP_SHEET, NEW_VARIANT_SHEET]`. The guide's
    `has_old_sheet` flag, the parts order, and the manifests all derive from this
    one signal → they cannot disagree.

    PURE — bytes arrive composited/fitted in the specs; the builder only
    base64-encodes them. Re-call with new bytes when the hard-guard rescales
    (atomic-unit invariant).
    """
    has_old = old_variant_spec is not None
    parts: list[dict] = [
        _image_part(crop_sheet_spec.image_bytes, crop_sheet_spec.mime_type)
    ]
    if old_variant_spec is not None:
        parts.append(
            _image_part(old_variant_spec.image_bytes, old_variant_spec.mime_type)
        )
    parts.append(_image_part(new_variant_spec.image_bytes, new_variant_spec.mime_type))

    guide_text = build_variant_image_guide(
        len(crops_with_roster), len(swap_targets), has_old
    )
    return BuiltReferences(
        parts=parts,
        guide_text=guide_text,
        count=len(parts),
        manifest_vars={
            "variant_manifest": build_variant_manifest(swap_targets, layout_cells),
            "crop_manifest": _render_crop_manifest(crops_with_roster),
        },
    )
