"""Shared reference-image + guide-text builder for Gemini image-gen prompts.

Single source of truth for HOW reference images are injected into a Gemini
prompt across the 7 `/api/illustration/*` endpoints and retouch `edit-object`.
Contract: `ai-storybook-design/api/libs/reference-prompt-builder.md`.

THE PROBLEM IT SOLVES — the old prompts relied on a POSITIONAL convention
("the first reference image is the BASE"). Inserting an art-style sheet shifted
every position, so the model's "Ảnh #k" map drifted off the real images.

THE INVARIANT — `build_references()` owns the `(parts, guide_text)` pair as ONE
ATOMIC UNIT. The "Ảnh #k" numbering in `guide_text` always matches the real
order of `parts`. The service/router MUST NOT insert / reorder / drop images
after calling the builder; if the image set changes, call the builder AGAIN.

PURE — no I/O, no fetch, no base64 decode, no Pillow, no AI. Callers resolve
(SSRF-guarded fetch / decode / composite) BEFORE constructing `ReferenceSpec`s.
The builder only encodes the already-fetched bytes into image-part dicts. Hence
NO `@traceable` here (LangSmith run-naming is done per-API at the service layer).

`parts[k]` reuses the existing image-part shape
(`illustration_generate_service._image_part`): `{"type":"image_url",
"image_url":"data:<mime>;base64,<b64>"}`. A tiny `_image_part` helper is COPIED
here (1 line) rather than imported so the builder stays free of a reverse
dependency on the service (avoids an import cycle). Cross-ref: keep the two
shapes in sync if the part contract ever changes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, NotRequired, TypedDict

__all__ = [
    "ReferenceRole",
    "ReferenceSpec",
    "BuiltReferences",
    "ROLE_USAGE",
    "ROLE_LABEL",
    "GUIDE_PREFIX",
    "build_references",
]


class ReferenceRole(str, Enum):
    """Semantic role of each reference image — drives ordering + guide wording."""

    BASE_VARIANT = "base_variant"        # variant: mandatory base image — keep identity
    ART_STYLE_SHEET = "art_style_sheet"  # composite contact-sheet from art_styles refs
    STYLE_REF = "style_ref"              # sketch base sheet 05/06 (mode A): user-supplied STYLE refs (referenceImages) = same-artist style anchor. REPLACES ART_STYLE_SHEET when present (2 sources of the SAME "art style reference" role, EXCLUSIVE). Sent SEPARATE (NO composite, ≤3 parts); NO-CONTENT (learn drawing voice only — never copy character/object/text/frame/layout)
    STAGE_VARIANT = "stage_variant"      # scene: optional stage ref — @stage mention (snapshot) OR stageVariantImageUrl fallback
    CHARACTER_SHEET = "character_sheet"  # scene: 1 composite sheet of all @character mentions (cell label = @mention)
    PROP_SHEET = "prop_sheet"            # scene: 1 composite sheet of all @prop mentions (cell label = @mention)
    LINEUP = "lineup"                    # compose_lineup_sheet composite (khung viền + badge + vạch 0.5m + fallback height 120/50cm + KHÔNG cap + pxPerCm tối ưu trần canvas). 04: tự attach ngoài builder (guide trong `references` JSON var). 08/09 (⚡2026-07-21 tối): đi QUA build_references nhưng chỉ dùng `parts` — guide_text không render vào seed → ROLE_USAGE dưới vẫn historical-only
    # PREVIOUS_SPREADS_SHEET removed 2026-07-21 — API 04 (the only consumer) dropped
    # the consistency sheet entirely (pencil style self-consistent); no consumer left.
    # STAGE_SHEET removed 2026-07-21 pm — 04 (the only consumer) now attaches the ONE
    # stage crop RAW (STAGE_VARIANT-tagged spec, no contact-sheet); no consumer left.
    ADDITIONAL = "additional"            # user-supplied extra refs (base64); sketch → here in v1
    SHEET_TEMPLATE = "sheet_template"     # sketch generate-*-sheet: STATIC layout frame (1 of 12 template-{N}.jpeg by cell count) — WHITE square cells = draw zones (1 variant each, read order L→R/T→B, each printed a gray ordinal 1..N locator), BLACK zones = skip; KEEP the cell frame/border around each item in the output (so cells are distinguishable) but never redraw the printed ordinal number (layout locator only)
    SOURCE = "source"                    # retouch: original image to edit
    REGION_MARK = "region_mark"          # retouch: marked-up SOURCE — region(s) to edit (set-of-mark)
    REMOVE_SHEET = "remove_sheet"        # retouch generate-background: composite sheet of objects to DELETE from SOURCE (cell label = #i name)
    SKETCH = "sketch"                    # DEFER v1 — declared; routed through ADDITIONAL usage
    # Remix profile (Phase 2 conform — reference-prompt-builder.md §Remix). The
    # remix swap builders render a BESPOKE guide, so these roles do NOT route
    # through `build_references()`/`ROLE_USAGE` v1 — they only drive the atomic
    # `(parts, guide_text, manifest_vars)` order inside the remix builders.
    CROP_SHEET = "crop_sheet"                  # sprite/mix: composed crop sheet (#1, layout template)
    HUMAN_REF = "human_ref"                    # sprite: per-object human appearance ref
    OLD_VARIANT_SHEET = "old_variant_sheet"    # mix: original-appearance locator sheet (omitted N=1)
    NEW_VARIANT_SHEET = "new_variant_sheet"    # mix: new-appearance target sheet


@dataclass(slots=True)
class ReferenceSpec:
    """One reference image, already fetched/decoded/composited into bytes.

    Service layer owns SSRF guard + size cap + Pillow composite BEFORE building
    a spec; the builder receives ready bytes only.
    """

    role: ReferenceRole
    image_bytes: bytes
    mime_type: str
    metadata: dict = field(default_factory=dict)  # {title?, index?, note?} — optional


class BuiltReferences(TypedDict):
    """Atomic output: ordered image parts + the guide text that describes them."""

    parts: list[dict]   # image-part dicts in order, WITHOUT the text part
    guide_text: str     # "Bản đồ ảnh tham chiếu: Ảnh #1 = ..., Ảnh #2 = ..."
    count: int          # len(parts) — lets the service re-check image budget
    # Remix profile (Phase 2 conform): the 2nd numbering axis — the "bake" cells
    # rendered as JSON prompt variables (sprite: {"cell_swap_plan"}; mix:
    # {"variant_manifest","crop_manifest"}). v1 illustration/retouch NEVER set it
    # (key absent is valid — `NotRequired`).
    manifest_vars: NotRequired[dict[str, str]]


# Guide prefix + per-role "Ảnh #{k} = <usage>" templates. Kept in Vietnamese —
# this text is sent to the model (the current prompt templates are Vietnamese).
# ART_STYLE_SHEET is the EMPHASISED variant (markdown bold + absolute language)
# to fight content-bleed: at variant endpoints the style sheet competes with the
# BASE image for attention, so STYLE-ONLY / NO-CONTENT must stand out.
GUIDE_PREFIX = "Bản đồ ảnh tham chiếu:"

ROLE_USAGE: dict[ReferenceRole, str] = {
    ReferenceRole.BASE_VARIANT: (
        "Ảnh #{k} = ẢNH BASE — GIỮ NGUYÊN nhận dạng (khuôn mặt/silhouette/tỷ lệ/"
        "layout). Chỉ thay yếu tố được mô tả trong variant."
    ),
    ReferenceRole.STAGE_VARIANT: (
        "Ảnh #{k} = bối cảnh tham chiếu (guide kèm @mention nếu có) — giữ nhận "
        "dạng địa điểm/atmosphere/ánh sáng/palette; scene là góc nhìn mới đặt "
        "nhân vật vào, KHÔNG copy nguyên."
    ),
    ReferenceRole.CHARACTER_SHEET: (
        "Ảnh #{k} = BẢNG NHÂN VẬT — mỗi ô là 1 nhân vật (nhãn ô = @mention, vd "
        "@kid/hero): NGUỒN nhận dạng nhân vật đó (khuôn mặt/tóc/trang phục/tỷ lệ). "
        "Đặt vào scene theo mô tả; KHÔNG vẽ khung bảng/nhãn ô/nền của bảng vào output."
    ),
    ReferenceRole.PROP_SHEET: (
        "Ảnh #{k} = BẢNG ĐẠO CỤ — mỗi ô là 1 đạo cụ (nhãn ô = @mention, vd "
        "@armor/base): NGUỒN nhận dạng đạo cụ đó (hình dạng/màu/chi tiết). Đặt vào "
        "scene theo mô tả tương tác (cầm/đặt/mặc); KHÔNG vẽ khung bảng/nhãn ô/nền "
        "của bảng vào output."
    ),
    ReferenceRole.ADDITIONAL: (
        "Ảnh #{k} = tham khảo bổ sung (sketch/ghi chú nghệ thuật) — tham khảo bố "
        "cục/ý tưởng, không bắt buộc sao chép."
    ),
    # LINEUP — ⚡2026-07-21 ORPHANED builder-side (04 composites + describes it in the
    # `references` JSON var, outside the builder; it is now identity+size, uncapped).
    # Historical 2026-07-19 relation-only wording kept for future reuse.
    ReferenceRole.LINEUP: (
        "Ảnh #{k} = BẢNG TƯƠNG QUAN KÍCH THƯỚC — các subject đứng cạnh nhau trên CÙNG 1 "
        "thước đo (vạch ngang cách nhau 0.5 m), badge SỐ tra entity_manifest (sheet=LINEUP, "
        "kèm height_cm): áp ĐÚNG tương quan kích thước giữa các subject trong scene (điều "
        "chỉnh theo phối cảnh xa-gần); KHÔNG copy cách xếp hàng ngang/tư thế đứng; KHÔNG "
        "vẽ vạch thước/baseline/badge số vào output."
    ),
    ReferenceRole.SOURCE: "Ảnh #{k} = ảnh gốc cần chỉnh sửa.",
    ReferenceRole.REGION_MARK: (
        "Ảnh #{k} = ẢNH ĐÁNH DẤU VÙNG CẦN CHỈNH SỬA — MỌI vùng được khoanh/tô "
        "(nét vẽ nổi bật; có thể có NHIỀU vùng) là KHU VỰC áp dụng yêu cầu. TẬP "
        "TRUNG chỉnh sửa trong các vùng đánh dấu; giữ các vùng khác gần như nguyên "
        "trạng. TUYỆT ĐỐI KHÔNG vẽ lại nét khoanh/dấu đánh dấu vào ảnh kết quả."
    ),
    # retouch generate-background — the REMOVE_SHEET is a by-example "delete list":
    # each cell is one object that MUST be erased from the SOURCE; the sheet itself
    # is a locator only, never drawn into the output. `metadata["labels"]` (the
    # `#i name` per cell) is appended by `build_references` so the model can join a
    # cell to its description in the `remove_objects` text block.
    ReferenceRole.REMOVE_SHEET: (
        "Ảnh #{k} = BẢNG ĐỐI TƯỢNG CẦN XOÁ — mỗi ô (nhãn `#i name`) là 1 đối tượng "
        "(character/prop) PHẢI bị loại khỏi ảnh nguồn. Tìm & xoá MỌI thể hiện của "
        "chúng, vẽ lại nền phía sau liền mạch (không bóng/silhouette/lỗ hổng). "
        "KHÔNG vẽ khung bảng/nhãn ô vào output."
    ),
    ReferenceRole.ART_STYLE_SHEET: (
        "Ảnh #{k} = **ART STYLE REFERENCE** (bảng tổng hợp phong cách). CHỈ tham "
        "chiếu PHONG CÁCH: độ dày nét (line weight), bảng màu (palette), cách đổ "
        "bóng (shading), độ mềm/độ tương phản. TUYỆT ĐỐI KHÔNG sao chép hay tham "
        "chiếu NỘI DUNG (nhân vật/vật thể/bố cục/cảnh) trong sheet — sheet chỉ "
        "minh hoạ style, không phải nội dung cần vẽ."
    ),
    # STYLE_REF (sketch base sheet 05/06, mode A) — the SAME-ARTIST sibling of
    # ART_STYLE_SHEET, EXCLUSIVE (one endpoint sends only ONE of the two). Each
    # user `referenceImages` item is a SEPARATE part (NO composite, ≤3). Like
    # ART_STYLE_SHEET it is NO-CONTENT, but framed as "YOU ARE the artist who drew
    # this ref" (spec 05 §Guide) so the model imitates the drawing voice exactly.
    # Wording copied VERBATIM from reference-prompt-builder.md §Guide.
    ReferenceRole.STYLE_REF: (
        "Ảnh #{k} = **ẢNH THAM CHIẾU PHONG CÁCH** — BẠN LÀ CHÍNH HOẠ SĨ đã vẽ ảnh "
        "này: BẮT CHƯỚC Y NGUYÊN giọng vẽ (chất nét/hatching/độ phủ bóng/mid-tone/"
        "cách tạo hình mặt-mắt-tóc hoặc hình khối/độ bo/texture giấy). **CHỈ học "
        "PHONG CÁCH — TUYỆT ĐỐI KHÔNG chép nhân vật/con vật/đồ vật/chữ/khung/bố cục** "
        "từ ảnh này vào output. Chỉ dẫn chữ nào VÊNH với ảnh tham chiếu → **ẢNH THẮNG**."
    ),
    # SHEET_TEMPLATE (sketch generate-*-sheet) — EMPHASISED like ART_STYLE_SHEET.
    # The template is a STATIC layout frame: WHITE square cells = draw zones,
    # BLACK zones = skip. Guide must fight two failure modes — drawing INTO black
    # zones, and REDRAWING the cell borders/frame/black bg into the output. Grid
    # dims (cols×rows, N) are appended per-spec by `build_references` from
    # `metadata.cell_count`; the per-cell item→cell binding is the `cell` field in
    # `{%request.variants%}` (single source of truth — Validation S1 Q3).
    ReferenceRole.SHEET_TEMPLATE: (
        "Ảnh #{k} = **KHUNG TEMPLATE BẮT BUỘC GIỮ NGUYÊN** (KHÔNG phải nội dung để vẽ "
        "lại). Kết quả PHẢI = CHÍNH ảnh template này, chỉ THÊM item vào trong các **ô "
        "VUÔNG TRẮNG**; vị trí ô + kích thước ô + tỉ lệ + đường lưới giữ TRÙNG KHÍT "
        "template (KHÔNG phóng to/thu nhỏ/xê dịch/bố trí lại). Mỗi ô vuông trắng (viền "
        "đen) = 1 vùng vẽ, có **SỐ in mờ (xám) ở GIỮA** đánh số 1..N theo thứ tự đọc "
        "**trái→phải, trên→xuống**; vẽ item `cell=i` vào ĐÚNG ô in số i, lấp gọn TRONG "
        "ô, KHÔNG tràn sang ô/vùng khác. ⚠️ **VÙNG TÔ ĐEN = NGOÀI VÙNG VẼ (KHÔNG phải "
        "nền): GIỮ ĐEN TUYỀN #000000, TUYỆT ĐỐI không để nét/màu/item/bóng/nền lan vào "
        "— dù 1 pixel.** ⚠️ **VIỀN Ô BẮT BUỘC & LIỀN MẠCH: mỗi ô có viền chữ nhật đen "
        "bao quanh; các ô KỀ NHAU DÙNG CHUNG cạnh, SÁT khít, KHÔNG hở/không khoảng "
        "trắng giữa các ô — đường lưới liên tục y như template.** Điểm khác template "
        "DUY NHẤT: **KHÔNG vẽ lại SỐ xám** (số chỉ là chỉ dẫn vị trí ô, KHÔNG phải "
        "nội dung)."
    ),
    # DEFER v1 — sketch is routed through ADDITIONAL usage so an accidental
    # SKETCH spec does not break the build (KISS, no separate flow).
    ReferenceRole.SKETCH: (
        "Ảnh #{k} = tham khảo bổ sung (sketch/ghi chú nghệ thuật) — tham khảo bố "
        "cục/ý tưởng, không bắt buộc sao chép."
    ),
    # documentation-only (contract §ROLE_USAGE remix rows) — the remix swap
    # builders render a BESPOKE guide and do NOT call `build_references()`, so
    # these entries never reach the v1 lookup path. Kept for a self-documenting,
    # complete contract table (Validation S1 Q3).
    ReferenceRole.CROP_SHEET: (
        "Ảnh #{k} = sheet gốc — lưới ô + số bake; GIỮ layout/số ô/pose/biểu cảm/"
        "art style; chỉ vẽ lại đối tượng được liệt kê."
    ),
    ReferenceRole.HUMAN_REF: (
        "Ảnh #{k} = visual chuẩn hoá của nhân vật — NGUỒN diện mạo để swap, KHÔNG "
        "vẽ khung này vào output; đặc điểm CẦN ÁP theo image_guide."
    ),
    ReferenceRole.OLD_VARIANT_SHEET: (
        "Ảnh #{k} = BẢNG DIỆN MẠO GỐC — ô số i = diện mạo GỐC của mục tiêu i "
        "(locator, KHÔNG vẽ vào output)."
    ),
    ReferenceRole.NEW_VARIANT_SHEET: (
        "Ảnh #{k} = BẢNG DIỆN MẠO MỚI — CÙNG số/vị trí với bảng GỐC; ô số i = "
        "diện mạo MỚI CẦN VẼ cho mục tiêu i."
    ),
}


# Thin per-role labels for `guide_style="map"` (opt-in 2026-07-22). A map line is
# a bare image→role bind (`Ảnh #k - {ROLE.name} ({ROLE_LABEL[role]})`); the "how to
# use each role" prose lives in the SEED for map consumers (keyed by role name, e.g.
# a `reference_use[]` block), so the builder only needs the short label here. Declare
# a role ONLY when an endpoint opts that role into map mode — edit-object (first
# adopter) uses SOURCE / REGION_MARK / ADDITIONAL. See reference-prompt-builder.md
# §Guide style modes.
ROLE_LABEL: dict[ReferenceRole, str] = {
    ReferenceRole.SOURCE: "ảnh gốc cần chỉnh sửa",
    ReferenceRole.REGION_MARK: "đánh dấu vùng cần chỉnh sửa",
    ReferenceRole.ADDITIONAL: "ảnh tham khảo",
}


def _image_part(data: bytes, mime: str) -> dict:
    """Copy of `illustration_generate_service._image_part` shape (kept local to
    avoid a service↔builder import cycle — see module docstring)."""
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": f"data:{mime};base64,{b64}"}


def _map_line(k: int, spec: ReferenceSpec) -> str:
    """One `guide_style="map"` line — a THIN image→role bind (+ optional per-image
    `description`), NOT the ROLE_USAGE prose (that lives in the seed for map mode).

    `ADDITIONAL` always renders the constant tag `ẢNH THAM KHẢO` — chốt user
    2026-07-22: EVERY reference carries it (description or not) so each ref binds
    the seed's single "ẢNH THAM KHẢO" entry directly (no bind-by-exclusion). Named
    roles (SOURCE / REGION_MARK / ...) print the UPPERCASE enum name so the tag
    matches the role key inside the seed's `reference_use[]`.
    """
    role = spec.role
    if role is ReferenceRole.ADDITIONAL:
        tag = "ẢNH THAM KHẢO"
    else:
        tag = f"{role.name} ({ROLE_LABEL.get(role, role.name)})"
    description = spec.metadata.get("description")
    return f"Ảnh #{k} - {tag}" + (f": {description}" if description else "")


def build_references(
    specs: list[ReferenceSpec], guide_style: Literal["verbose", "map"] = "verbose"
) -> BuiltReferences:
    """Build ordered image parts + a matching "Ảnh #k" guide text, atomically.

    Parts are emitted in the EXACT order of `specs` (the service has already
    sorted them per the ordering contract — the builder NEVER reorders). The
    guide numbers each image 1-based, k == its position among the images (the
    service prepends the text part at index 0, so "Ảnh #1" == the first image).

    `guide_style` (opt-in, default `"verbose"`) ONLY changes the `guide_text`
    FORMAT — `parts` / their order / `count` are IDENTICAL across styles:
      - `"verbose"` (default, 0 churn for every existing consumer): each line is
        the full ROLE_USAGE prose (`Ảnh #k = <how to use this role>`).
      - `"map"`: each line is a THIN bind (`Ảnh #k - <role tag>[: <description>]`)
        — used when the seed already carries the per-role usage prose (keyed by
        role name), so the builder only supplies the image→role map.

    Empty `specs` → empty guide (graceful: an endpoint with no images at all).
    """
    if not specs:
        return BuiltReferences(parts=[], guide_text="", count=0)

    parts: list[dict] = []
    lines: list[str] = []
    for k, spec in enumerate(specs, start=1):
        parts.append(_image_part(spec.image_bytes, spec.mime_type))
        if guide_style == "map":
            lines.append(_map_line(k, spec))
            continue
        usage = ROLE_USAGE.get(spec.role, ROLE_USAGE[ReferenceRole.ADDITIONAL])
        # Per-spec usage override (additive): a spec may carry `metadata["usage"]` to
        # repurpose a shared role's wording for one endpoint WITHOUT forking the role
        # enum — e.g. detect-objects reuses SOURCE but means "locate", not "edit".
        # Only set when present, so every existing caller/role is unchanged.
        usage = spec.metadata.get("usage") or usage
        line = usage.format(k=k)
        # Entity sheets (CHARACTER_SHEET/PROP_SHEET) carry `metadata.labels` = the @mention
        # per cell, in order → append so the model can join each cell to its mention. Additive:
        # only entity-sheet specs set `labels`, so every other role/endpoint is unchanged.
        labels = spec.metadata.get("labels")
        if labels:
            line += " (các ô: " + ", ".join(labels) + ")"
        # SHEET_TEMPLATE (sketch) carries grid dims `metadata={cols,rows,cell_count}`
        # → append the layout summary so the model knows the grid shape + cell count.
        # Additive: only SHEET_TEMPLATE specs set `cell_count`, so every other role/
        # endpoint is unchanged. Per-cell item→cell binding stays in the
        # `{%request.variants%}` `cell` field — grid dims ONLY here (Validation S1 Q3).
        cell_count = spec.metadata.get("cell_count")
        if cell_count:
            cols = spec.metadata.get("cols")
            rows = spec.metadata.get("rows")
            line += f" (lưới {cols}×{rows}, {cell_count} ô — ô số i = variant thứ i)"
        lines.append(line)

    guide_text = GUIDE_PREFIX + "\n" + "\n".join(lines)
    return BuiltReferences(parts=parts, guide_text=guide_text, count=len(parts))
