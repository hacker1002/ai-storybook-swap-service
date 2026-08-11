"""Unit — `nearest_aspect_ratio` (ported for the edit-object region guard, P3c)."""

from __future__ import annotations

import pytest

from src.services.image_ops import nearest_aspect_ratio


@pytest.mark.parametrize(
    "w,h,expected",
    [
        (1000, 1000, "1:1"),
        (1600, 900, "16:9"),
        (900, 1600, "9:16"),
        (1920, 1080, "16:9"),
        (2100, 900, "21:9"),
        (800, 1200, "2:3"),
        (1200, 800, "3:2"),
    ],
)
def test_nearest_enum(w, h, expected):
    assert nearest_aspect_ratio(w, h) == expected


def test_near_square_slight_landscape_snaps_to_1_1():
    # Small deviation from square lands on 1:1 (nearest by relative error).
    assert nearest_aspect_ratio(1010, 1000) == "1:1"
