"""Direct core test for `compose_crop_sheet` — REAL Pillow composition.

Anchors the stateless CV path independent of the HTTP layer: only the SSRF-guarded
fetch is stubbed; the frame render + canvas compose run for real.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.models.requests.build_crop_sheet import BuildCropSheetRequest
from src.services.remix import crop_sheet_composer
from src.services.remix.errors import RemixDomainError


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (40, 40), (10, 120, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _req(n: int = 3) -> BuildCropSheetRequest:
    return BuildCropSheetRequest.model_validate(
        {
            "sheet_geometry": {"width": 128 * n, "height": 128},
            "crops": [
                {
                    "id": f"c{i}",
                    "media_url": f"https://example.test/c{i}.png",
                    "geometry": {"x": i * 128, "y": 0, "w": 128, "h": 128},
                }
                for i in range(n)
            ],
        }
    )


@pytest.mark.asyncio
async def test_compose_crop_sheet_real(monkeypatch):
    async def _fetch(url: str):
        return _tiny_png(), "image/png"

    monkeypatch.setattr(crop_sheet_composer, "fetch_image_bytes", _fetch)
    result = await crop_sheet_composer.compose_crop_sheet(_req(3))
    assert result.composed_count == 3
    assert result.skipped == []
    img = Image.open(io.BytesIO(result.png_bytes))
    assert img.format == "PNG"
    assert (result.width, result.height) == (img.width, img.height)


@pytest.mark.asyncio
async def test_compose_crop_sheet_all_failed(monkeypatch):
    async def _boom(url: str):
        raise RuntimeError("down")

    monkeypatch.setattr(crop_sheet_composer, "fetch_image_bytes", _boom)
    with pytest.raises(RemixDomainError) as ei:
        await crop_sheet_composer.compose_crop_sheet(_req(2))
    assert ei.value.code == "ALL_CROPS_FAILED"
    assert ei.value.status == 422
