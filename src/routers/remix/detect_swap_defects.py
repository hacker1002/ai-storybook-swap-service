"""POST /api/remix/detect-swap-defects handler (thin wrapper over the core).

Swap defect localization: 1 Gemini multimodal call locates wrong/poor swap regions
on the result sheet → box 0-1000 → server px circle. Stateless, advisory,
`X-API-Key`. Caller (validation S1) = FE DIRECT HTTP — the "check" button per
batch/sheet in the Remix batch sidebar (NOT job 02).

All validation lives in the Pydantic model: body/cross-field failures raise
`RemixDomainError` (400) and the core raises it for image/Gemini failures — both
surface through the GLOBAL `RemixDomainError` handler (main.py) as the spec
envelope. So the handler stays thin (no local try/except), parity with
`detect_crop_geometry`.

NO FE consumer today — internal/test/debug only (the job pipeline calls the
`run_*` cores in-process). Auth is the editor-session Bearer dep enforced at the
router-group level (`src/routers/remix/router.py`), NOT `X-API-Key`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from src.models.requests.detect_swap_defects import (
    DetectSwapDefectsData,
    DetectSwapDefectsRequest,
    DetectSwapDefectsResponse,
)
from src.services.ai_usage import AiCallContext
from src.services.remix.detect_swap_defects_core import run_detect_swap_defects

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/detect-swap-defects", response_model=DetectSwapDefectsResponse)
async def detect_swap_defects(
    req: DetectSwapDefectsRequest,
) -> DetectSwapDefectsResponse:
    # PII discipline: counts only — never URLs / human data.
    logger.info(
        "detect_swap_defects_start cells=%d objects=%d",
        len(req.crops), len(req.swap_objects),
    )
    # AI-usage attribution (Phase 05): optional remixId → remix cost bucket.
    result = await run_detect_swap_defects(
        req, ai_context=AiCallContext(remix_id=req.remixId)
    )
    logger.info(
        "detect_swap_defects_ok defects=%d truncated=%s",
        result.meta.defectCount, bool(result.meta.truncated),
    )
    return DetectSwapDefectsResponse(
        success=True,
        data=DetectSwapDefectsData(defects=result.defects),
        meta=result.meta,
    )
