"""POST /api/editor/assets (P3c Gap 1) — proxy upload for the remix sub-app.

WHY: the editor's eraser tab draws a mask client-side then uploads it STRAIGHT to
Supabase Storage via supabase-js. The sub-app has NO supabase-js seam (SRS §3.1),
so that write path is broken. This endpoint is the minimal proxy (validation
260811, option A): the sub-app POSTs the image as base64 with its editor-session
Bearer; the service validates + uploads into App Storage + returns the public URL.

Transport = JSON base64 (data-URI or raw) — uniform with this service's other image
endpoints (rmbg/edit-object), no `python-multipart` dependency. Signed-URL upload is
noted as an additive upgrade path (validation 260811, option B).

SECURITY (new write surface — hardened): Bearer auth (router group) + MIME allowlist
(content-SNIFFED from the decoded bytes — a spoofed `mimeType` is ignored) + size cap
+ the storage path is generated ENTIRELY server-side (never client-supplied → no
traversal / overwrite) + admin_ref/sid audit log.

ENVELOPE: `/api/editor/*` `ServiceError` shape (NOT image-api's).
"""

from __future__ import annotations

import base64
import binascii

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from src.auth.editor_session import EditorSessionContext, require_editor_session
from src.core.errors import ServiceError, validation_error
from src.core.logging import get_logger
from src.services.image_ops import sniff_mime
from src.services.storage import StorageUploadError, build_editor_asset_path, upload_bytes

logger = get_logger("editor.upload_asset")

# Allowlisted upload types (parity with the retouch/rmbg input allowlist). The
# sniffed bytes are authoritative — any client-declared type is ignored.
_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}
# Asset cap (mask/composite images are small). The app-level Content-Length guard
# rejects grossly over-cap bodies earlier; this is the authoritative per-asset cap.
_MAX_ASSET_BYTES = 10 * 1024 * 1024
_DATA_URI_PREFIX = "data:"


class UploadAssetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Data-URI (`data:image/png;base64,...`) OR raw base64. The MIME is decided by
    # sniffing the decoded bytes, NOT by any data-URI header (anti-spoof).
    imageBase64: str = Field(min_length=1)


def _decode(image_b64: str) -> bytes:
    """Decode data-URI/raw base64 → bytes. Raises `validation_error` on any failure
    or over-cap payload (checked BEFORE the expensive sniff)."""
    raw = image_b64.strip()
    if raw.startswith(_DATA_URI_PREFIX):
        _, _, payload = raw.partition(",")
        if not payload:
            raise validation_error("Malformed data URI (empty payload)")
        raw = payload
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise validation_error("Malformed base64") from exc
    if not data:
        raise validation_error("Empty upload")
    if len(data) > _MAX_ASSET_BYTES:
        raise validation_error(
            "File exceeds size cap",
            details={"bytes": len(data), "cap": _MAX_ASSET_BYTES},
        )
    return data


async def upload_asset(
    params: UploadAssetParams,
    session: EditorSessionContext = Depends(require_editor_session),
) -> dict:
    body = _decode(params.imageBase64)

    # Sniff the ACTUAL bytes — never trust a client-declared type (a data-URI header
    # claiming image/png wrapping other bytes is rejected).
    mime = sniff_mime(body[:256])
    if mime not in _ALLOWED_MIMES:
        raise validation_error(
            "Unsupported media type — only png/jpeg/webp",
            details={"detectedType": mime},
        )

    # Path is 100% server-generated (ts + random + allowlist ext). The client never
    # supplies a path → no traversal, no overwrite of another object.
    storage_path = build_editor_asset_path(mime)
    try:
        public_url = await upload_bytes(storage_path, body, content_type=mime)
    except StorageUploadError as exc:
        logger.error(
            "editor_asset_upload_failed",
            extra={"data": {"admin_ref": session.admin_ref, "path": storage_path}},
        )
        raise ServiceError("INTERNAL_ERROR", 500, "Storage upload failed") from exc

    logger.info(
        "editor_asset_uploaded",
        extra={
            "data": {
                "admin_ref": session.admin_ref,
                "sid": session.sid,
                "path": storage_path,
                "bytes": len(body),
                "mime": mime,
            }
        },
    )
    return {
        "success": True,
        "data": {
            "url": public_url,
            "storagePath": storage_path,
            "contentType": mime,
            "bytes": len(body),
        },
    }
