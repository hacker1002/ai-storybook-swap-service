"""Unit — `build_editor_asset_path` is server-side + traversal-proof (P3c Gap 1)."""

from __future__ import annotations

import re

from src.services.storage import build_editor_asset_path


def test_prefix_and_ext_by_mime():
    assert build_editor_asset_path("image/png").endswith(".png")
    assert build_editor_asset_path("image/jpeg").endswith(".jpg")
    assert build_editor_asset_path("image/webp").endswith(".webp")
    for mime in ("image/png", "image/jpeg", "image/webp"):
        assert build_editor_asset_path(mime).startswith("editor-assets/")


def test_unknown_mime_falls_back_to_png_ext():
    # The route validates MIME before calling this; the builder degrades safely.
    assert build_editor_asset_path("application/octet-stream").endswith(".png")


def test_path_shape_no_client_input():
    # `{prefix}/{ts_ms}-{8hex}.{ext}` — nothing client-supplied, so no `..` or slashes
    # beyond the single prefix separator (traversal/overwrite guard).
    path = build_editor_asset_path("image/png")
    assert re.fullmatch(r"editor-assets/\d+-[0-9a-f]{8}\.png", path), path
    assert ".." not in path


def test_uniqueness_within_same_ms():
    # 8 hex random → collisions within one millisecond are astronomically unlikely.
    paths = {build_editor_asset_path("image/png") for _ in range(50)}
    assert len(paths) == 50
