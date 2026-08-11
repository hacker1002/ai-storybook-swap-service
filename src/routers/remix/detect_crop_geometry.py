"""POST /api/remix/detect-crop-geometry handler (thin wrapper over the core).

2-step crop-frame detection: Step-1 numpy `detect_frames_anchored` (ANCHORED
per-cell snap) + Step-2 Gemini classify (frame_index → number). Box from numpy (the
detected frame VERBATIM, no ratio reshape), number from vision (catches cell
reorder). Stateless,
`X-API-Key`. The job cut pipeline (02/05)
calls `run_detect_crop_geometry` in-process instead of hitting this route.

Custom preconditions (geometry within sheet dims, `target_numbers ⊆ crops`) → 422
`RemixDomainError` (the global handler envelopes it). Pydantic body errors → 400.

NO FE consumer today — internal/test/debug only (the job pipeline calls the
`run_*` cores in-process). Auth is the editor-session Bearer dep enforced at the
router-group level (`src/routers/remix/router.py`), NOT `X-API-Key`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.editor_session import EditorSessionContext, require_editor_session

from src.models.requests.detect_crop_geometry import (
    DetectCropGeometryData,
    DetectCropGeometryRequest,
    DetectCropGeometryResponse,
)
from src.services.ai_usage import AiCallContext
from src.services.remix.detect_crop_geometry_service import run_detect_crop_geometry
from src.services.remix.errors import RemixDomainError

router = APIRouter()


@router.post("/detect-crop-geometry", response_model=DetectCropGeometryResponse)
async def detect_crop_geometry(
    req: DetectCropGeometryRequest,
    session: EditorSessionContext = Depends(require_editor_session),
) -> DetectCropGeometryResponse:
    dims = req.original_sheet_dimensions

    # Custom precondition: every crop geometry must fit inside the sheet (422).
    for c in req.crops:
        g = c.geometry
        if g.x + g.w > dims.width or g.y + g.h > dims.height:
            raise RemixDomainError(
                status=422,
                code="VALIDATION_ERROR",
                message="crop geometry exceeds sheet dimensions",
                details={"number": c.number},
            )

    # Custom precondition: target_numbers ⊆ crops[].number (422).
    if req.target_numbers is not None:
        valid = {c.number for c in req.crops}
        unknown = sorted({n for n in req.target_numbers if n not in valid})
        if unknown:
            raise RemixDomainError(
                status=422,
                code="VALIDATION_ERROR",
                message="target_numbers must be a subset of crops[].number",
                details={"target_numbers": unknown},
            )

    # AI-usage attribution (Phase 05): optional remixId → remix cost bucket.
    result = await run_detect_crop_geometry(
        req,
        ai_context=AiCallContext(
            remix_id=req.remixId, admin_ref=session.admin_ref, sid=session.sid
        ),
    )
    return DetectCropGeometryResponse(
        success=True,
        data=DetectCropGeometryData(detections=result.detections),
        meta=result.meta,
    )
