"""AI-usage logging layer for the Remix Swap Service.

Ported from `ai-storybook-image-api/src/services/ai_usage/*` (Phase 03,
`plans/260810-1811-remix-swap-p3b-jobs-pipeline-port/phase-03-ai-usage-logging-port.md`).

Fidelity: `pricing.py` + `sanitize.py` are pure logic ported verbatim (versioned
price table untouched). Two deliberate divergences from image-api, forced by this
service's DB seam:

  1. The DB write goes through `get_adapter().insert_ai_log(row)` (asyncpg) — NOT
     `supabase.table(...).insert(...)`. Because that call is async, the fire-and-
     forget path schedules an `asyncio` task instead of a thread-executor job.
  2. Attribution: `user_id` is ALWAYS NULL (App DB has no user directory); the
     admin actor + editor session ids ride nested under `request.audit`, together
     with `source="remix-swap-service"` (the cost discriminator vs the editor app,
     since both write the SAME `ai_service_logs` table).

Public surface mirrors image-api so choke points (Phase 05/06) port cleanly.
"""

from src.services.ai_usage.context import AiCallContext
from src.services.ai_usage.logger import (
    AiLogEntry,
    drain,
    log_ai_request,
    new_request_id,
)
from src.services.ai_usage.pricing import PRICING_VERSION, compute_cost
from src.services.ai_usage.sanitize import (
    OUTPUT_PREFIX,
    build_ref_metadata,
    extract_output_blobs,
    extract_ref_blobs,
    sanitize_request,
    sanitize_response,
)

__all__ = [
    "AiCallContext",
    "AiLogEntry",
    "drain",
    "log_ai_request",
    "new_request_id",
    "PRICING_VERSION",
    "compute_cost",
    "OUTPUT_PREFIX",
    "build_ref_metadata",
    "extract_output_blobs",
    "extract_ref_blobs",
    "sanitize_request",
    "sanitize_response",
]
