"""GET /api/provenance/ai-request-references/{aiRequestId} (P3c Gap 2).

Lists the **reference images** sent to the AI provider in one past call, looked up
by `ai_service_logs.id` (the `ai_request_id` stamped on every AI-generated
`illustrations[]` entry). Lets the Inpaint tab re-offer exactly the ref set of the
previous generate instead of re-picking/uploading.

Read-only: no DB write, no AI call, **no usage logging**.

AUTHZ DELTA vs image-api (validation 260811): image-api gates on Supabase user JWT +
`admin ∨ require_book_access(book)`. The editor session is ALREADY role-wide admin
(spec 00 requires `role=admin`), so the per-book gate is meaningless here — this
endpoint is an **existence check only** (bỏ `require_book_access`, bỏ nhánh remap
404→403; no book resolution hop). Not found → 404; non-UUID path → 400
(RequestValidationError → VALIDATION_ERROR). Row present → refs returned.

`map_ref_files` is ported VERBATIM from image-api (pure JSONB-shape logic).

Spec: `ai-storybook-design/api/provenance/01-get-ai-request-references.md`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter

from src.db.adapter import get_adapter
from src.routers._shared.deps import error_response

logger = logging.getLogger(__name__)

router = APIRouter()

# `ref_files[]` optional keys copied to the response: (source key, output key).
# `url` is handled separately (the required key). Anything else the logger writes
# (e.g. `deduped`) is intentionally NOT forwarded.
_REF_OPTIONAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("mime", "mimeType"),
    ("bytes", "bytes"),
    ("sha256", "sha256"),
)


def map_ref_files(request: Any) -> tuple[list[dict[str, Any]], int, int]:
    """`request.ref_files[]` → `(images, total, skipped)`.

    `index` is 1-based over the ORIGINAL array and is assigned BEFORE filtering, so
    dropping a middle entry leaves a gap (3 entries, #2 unusable → indexes 1 and 3) —
    the numbers stay aligned with the original prompt's image map.

    An entry without a usable `url` (upload-failed ref: only `sha256`/`bytes`) is
    dropped and counted in `skipped`. Fully defensive about the JSONB shape: a
    non-dict `request` or non-list `ref_files` yields `([], 0, 0)` rather than a 500.
    """
    refs = request.get("ref_files") if isinstance(request, dict) else None
    if not isinstance(refs, list):
        refs = []

    images: list[dict[str, Any]] = []
    skipped = 0
    for index, entry in enumerate(refs, start=1):
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url:
            skipped += 1
            continue
        image: dict[str, Any] = {"index": index, "url": url}
        for src_key, out_key in _REF_OPTIONAL_FIELDS:
            value = entry.get(src_key)
            if value is not None:
                image[out_key] = value
        images.append(image)

    return images, len(refs), skipped


@router.get("/ai-request-references/{ai_request_id}")
async def get_ai_request_references(ai_request_id: uuid.UUID) -> dict:
    """Reference images of one past AI call. 404 (purged/unknown id) is a NORMAL
    outcome the caller degrades on — never a blocking error."""
    log_id = str(ai_request_id)

    row = await get_adapter().get_ai_log(ai_request_id)
    if row is None:
        logger.info("provenance_refs_not_found ai_request_id=%s", log_id)
        raise error_response(404, "NOT_FOUND", "AI request log not found")

    images, total, skipped = map_ref_files(row.get("request"))
    logger.info(
        "provenance_refs_ok ai_request_id=%s images=%d total=%d skipped=%d",
        log_id, len(images), total, skipped,
    )
    return {
        "success": True,
        "data": {
            "aiRequestId": log_id,
            "operation": row.get("operation"),
            "provider": row.get("provider"),
            "model": row.get("model"),
            "status": row.get("status"),
            "createdAt": row.get("created_at"),
            "images": images,
        },
        "meta": {"totalRefFiles": total, "skippedCount": skipped},
    }
