"""POST /api/remix/detect-rmbg-defects handler (thin wrapper over the core).

Remove-bg defect localization: 1 Gemini multimodal call locates wrong/poor
remove-background regions on the recomposed RESULT sheet (RGBA transparent) vs the
ORIGINAL still-background sheet → box 0-1000 → server px circle. Stateless,
advisory, `X-API-Key`. 3rd plane of the detect family (sprite 06 / mix 07).
Caller: job 13 (in-process core) + future FE "khoanh vùng lỗi tách nền" (tab
Remove BG).

All validation lives in the Pydantic model: body/cross-field failures raise
`RemixDomainError` (400) and the core raises it for image/Gemini failures — both
surface through the GLOBAL `RemixDomainError` handler (main.py) as the spec
envelope. So the handler stays thin (no local try/except), parity with
`detect_mix_defects`.

NO FE consumer today — internal/test/debug only (the job pipeline calls the
`run_*` cores in-process). Auth is the editor-session Bearer dep enforced at the
router-group level (`src/routers/remix/router.py`), NOT `X-API-Key`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from src.models.requests.detect_rmbg_defects import (
    DetectRmbgDefectsData,
    DetectRmbgDefectsRequest,
    DetectRmbgDefectsResponse,
)
from src.services.ai_usage import AiCallContext
from src.services.remix.detect_rmbg_defects_core import run_detect_rmbg_defects

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/detect-rmbg-defects", response_model=DetectRmbgDefectsResponse)
async def detect_rmbg_defects(
    req: DetectRmbgDefectsRequest,
) -> DetectRmbgDefectsResponse:
    # PII discipline: counts only — never URLs / image data.
    logger.info(
        "detect_rmbg_defects_start cells=%d result_cells=%d",
        len(req.crops), len(req.result_crops),
    )
    # AI-usage attribution (Phase 05): optional remixId → remix cost bucket.
    result = await run_detect_rmbg_defects(
        req, ai_context=AiCallContext(remix_id=req.remixId)
    )
    logger.info(
        "detect_rmbg_defects_ok defects=%d truncated=%s",
        result.meta.defectCount, bool(result.meta.truncated),
    )
    return DetectRmbgDefectsResponse(
        success=True,
        data=DetectRmbgDefectsData(defects=result.defects),
        meta=result.meta,
    )
