"""POST /api/remix/detect-mix-defects handler (thin wrapper over the core).

Mix-swap defect localization: 1 Gemini multimodal call locates wrong/poor swap
regions on the recomposed mix RESULT sheet → box 0-1000 → server px circle.
Stateless, advisory, `X-API-Key`. Sibling of `detect_swap_defects` for the MIX
plane. Caller: job 12 (in-process core) + future FE "khoanh vùng lỗi swap mix".

All validation lives in the Pydantic model: body/cross-field failures raise
`RemixDomainError` (400) and the core raises it for image/Gemini failures — both
surface through the GLOBAL `RemixDomainError` handler (main.py) as the spec
envelope. So the handler stays thin (no local try/except), parity with
`detect_swap_defects`.

NO FE consumer today — internal/test/debug only (the job pipeline calls the
`run_*` cores in-process). Auth is the editor-session Bearer dep enforced at the
router-group level (`src/routers/remix/router.py`), NOT `X-API-Key`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.auth.editor_session import EditorSessionContext, require_editor_session

from src.models.requests.detect_mix_defects import (
    DetectMixDefectsData,
    DetectMixDefectsRequest,
    DetectMixDefectsResponse,
)
from src.services.ai_usage import AiCallContext
from src.services.remix.detect_mix_defects_core import run_detect_mix_defects

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/detect-mix-defects", response_model=DetectMixDefectsResponse)
async def detect_mix_defects(
    req: DetectMixDefectsRequest,
    session: EditorSessionContext = Depends(require_editor_session),
) -> DetectMixDefectsResponse:
    # PII discipline: counts only — never URLs / human data.
    logger.info(
        "detect_mix_defects_start cells=%d targets=%d",
        len(req.crops), len(req.swap_targets),
    )
    # AI-usage attribution (Phase 05): optional remixId → remix cost bucket.
    result = await run_detect_mix_defects(
        req,
        ai_context=AiCallContext(
            remix_id=req.remixId, admin_ref=session.admin_ref, sid=session.sid
        ),
    )
    logger.info(
        "detect_mix_defects_ok defects=%d has_old=%s truncated=%s",
        result.meta.defectCount, bool(result.meta.hasOldVariantSheet),
        bool(result.meta.truncated),
    )
    return DetectMixDefectsResponse(
        success=True,
        data=DetectMixDefectsData(defects=result.defects),
        meta=result.meta,
    )
