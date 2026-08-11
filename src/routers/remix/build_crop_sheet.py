"""POST /api/remix/build-crop-sheet — stateless composer router.

Thin handler: auth → call `compose_crop_sheet()` → branch base64/binary.
Per-crop failures live inside `data.skipped[]` with HTTP 200 (partial-success).
Only validation / all-failed / unhandled errors are 4xx/5xx.

NO FE consumer today — internal/test/debug only (the job pipeline calls the
`run_*` cores in-process). Auth is the editor-session Bearer dep enforced at the
router-group level (`src/routers/remix/router.py`), NOT `X-API-Key`.
"""

import base64
import logging
import time
from typing import Union

from fastapi import APIRouter, Response

from src.models.requests.build_crop_sheet import (
    BuildCropSheetData,
    BuildCropSheetMeta,
    BuildCropSheetRequest,
    BuildCropSheetResponse,
)
from src.routers._shared.deps import error_response
from src.services.remix.crop_sheet_composer import compose_crop_sheet
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/build-crop-sheet", response_model=None)
async def build_crop_sheet(
    body: BuildCropSheetRequest,
) -> Union[BuildCropSheetResponse, Response]:
    t0 = time.monotonic()
    try:
        result = await compose_crop_sheet(body)
    except RemixDomainError:
        # Surface via app-level handler in main.py (uniform envelope shape).
        raise
    except Exception:
        logger.exception("build_crop_sheet_unhandled")
        raise error_response(500, "INTERNAL_ERROR", "Unexpected error during composition")

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if body.response_format == "binary":
        return Response(
            content=result.png_bytes,
            media_type="image/png",
            headers={
                "X-Composed-Count": str(result.composed_count),
                "X-Skipped-Count": str(len(result.skipped)),
                "X-Sheet-Width": str(result.width),
                "X-Sheet-Height": str(result.height),
            },
        )

    b64 = base64.b64encode(result.png_bytes).decode("ascii")
    return BuildCropSheetResponse(
        success=True,
        data=BuildCropSheetData(
            image_base64=b64,
            mime_type="image/png",
            width=result.width,
            height=result.height,
            composed_count=result.composed_count,
            skipped=result.skipped,
        ),
        meta=BuildCropSheetMeta(
            processingTime=elapsed_ms,
            fetchMs=result.fetch_ms,
            compositeMs=result.composite_ms,
            inputBytes=result.input_bytes,
        ),
    )
