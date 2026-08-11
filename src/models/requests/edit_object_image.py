"""Pydantic models + constants for POST /api/retouch/edit-object-image (P3c port).

Ported VERBATIM from `ai-storybook-image-api/src/models/requests/edit_object_image.py`.
Gemini image editing with persistent Storage upload. In this service the public
HTTP layer IS mounted (unlike the rmbg/upscale core-only ports) — the remix
sub-app's Inpaint tab calls this endpoint. Auth is editor-session Bearer at the
router group level (NOT X-API-Key); the model contract is byte-identical to
image-api so the shared FE client stays untouched.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from src.models.requests._attribution import RemixId, SnapshotId
from src.services.resource_persist import SaveResourceDirective

logger = logging.getLogger(__name__)

__all__ = [
    "EditObjectImageParams",
    "EditObjectImageData",
    "EditObjectImageMeta",
    "EditObjectImageResponse",
    "EditObjectModelParams",
    "EditObjectModelParamsInner",
    "ReferenceImage",
    "GEMINI_TEMPERATURE",
    "GEMINI_TIMEOUT_S",
    "MAX_PROMPT_LENGTH",
    "MAX_REFERENCE_IMAGES",
    "MAX_IMAGE_BYTES",
    "MAX_DESCRIPTION_LENGTH",
    "VALID_MIME_TYPES",
    "STORAGE_EDIT_OBJECT_PREFIX",
    "EDIT_OBJECT_IMAGE_SYSTEM_NAME",
    "ERROR_CODES",
]


# Endpoint temperature default (the DEFAULT model comes from the seed row's
# `prompt_templates.model`, resolved per-request — no hardcoded model id here).
GEMINI_TEMPERATURE: float = 0.3
GEMINI_TIMEOUT_S: float = 150.0

MAX_PROMPT_LENGTH: int = 2000
MAX_REFERENCE_IMAGES: int = 5
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024
MAX_DESCRIPTION_LENGTH: int = 200

VALID_MIME_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp")

STORAGE_EDIT_OBJECT_PREFIX: str = "edit-objects"

# `prompt_templates.name` of the seed system prompt (redesign 2026-07-22 — the
# router fetches this row + renders `{%request.prompt%}` / `{%request.reference_guide%}`).
# DEFAULT model is read from the row's `model` column (change-model-without-deploy).
EDIT_OBJECT_IMAGE_SYSTEM_NAME: str = "EDIT_OBJECT_IMAGE_SYSTEM"

ERROR_CODES: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "SSRF_BLOCKED": 400,
    "INVALID_API_KEY": 403,
    "PROMPT_TEMPLATE_NOT_FOUND": 500,
    "IMAGE_FETCH_ERROR": 422,
    "SAFETY_FILTER_BLOCKED": 422,
    "REGION_ASPECT_MISMATCH": 422,
    "UNSUPPORTED_MODEL": 422,
    "GEMINI_RATE_LIMIT": 429,
    "NO_IMAGE_RESPONSE": 502,
    "GEMINI_ERROR": 502,
    "STORAGE_UPLOAD_ERROR": 500,
    "TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}


def _sanitize_description(v: str | None) -> str | None:
    """Strip C0/DEL control chars (keep unicode) + collapse blank → None. Cap at
    `MAX_DESCRIPTION_LENGTH` (raise → 400, parity with the `prompt` validator).

    `description` is a PUBLIC, untrusted input rendered into the prompt map line
    (`Ảnh #k - ẢNH THAM KHẢO: <description>`). Control chars are dropped so a
    newline/tab can't fracture the single-line map; it is NEVER used in a
    path/SQL, so prompt-injection is inherent-and-accepted (same as @mentions).
    """
    if v is None:
        return None
    cleaned = "".join(ch for ch in v if ch >= " " and ch != "\x7f").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"description exceeds {MAX_DESCRIPTION_LENGTH} chars")
    return cleaned


class ReferenceImage(BaseModel):
    model_config = {"extra": "forbid"}

    base64Data: str = Field(min_length=1)
    mimeType: Literal["image/png", "image/jpeg", "image/webp"]
    # per-image label rendered into the map guide line for a SYSTEM ref. User
    # uploads omit it → the map line keeps the bare "ẢNH THAM KHẢO" tag.
    description: str | None = None

    @field_validator("base64Data")
    @classmethod
    def _check_decoded_size(cls, v: str) -> str:
        # Heuristic: base64 length * 0.75 ≈ decoded bytes (ignoring padding).
        if len(v) * 0.75 > MAX_IMAGE_BYTES:
            raise ValueError(f"base64Data exceeds {MAX_IMAGE_BYTES} bytes decoded")
        return v

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str | None) -> str | None:
        return _sanitize_description(v)


class EditObjectModelParamsInner(BaseModel):
    """Inner `params` of `modelParams` — typed because the resolver READS
    `temperature`. `extra="forbid"` → a stray inner key trips a loud 400."""

    model_config = {"extra": "forbid"}

    temperature: float | None = None  # [0,2]; default 0.3; out-of-range → clamp


class EditObjectModelParams(BaseModel):
    """`modelParams` for edit-object — caller model override (group `edit-object`).

    `model` is the PUBLIC allowlist id (key into `PUBLIC_TO_GEMINI_IMAGE`), resolved
    to a Gemini dispatch id by `resolve_gemini_model` (group `edit-object`).
    """

    model_config = {"extra": "forbid"}

    model: str = Field(min_length=1)
    params: EditObjectModelParamsInner | None = None


class EditObjectImageParams(BaseModel):
    model_config = {"extra": "forbid"}

    prompt: str
    imageUrl: HttpUrl
    referenceImages: list[ReferenceImage] | None = Field(
        default=None, max_length=MAX_REFERENCE_IMAGES
    )
    aspectRatio: Literal[
        "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
    ] = "1:1"
    imageSize: Literal["1K", "2K", "4K"] = "2K"
    # Set-of-mark visual prompting: SOURCE with the edit region(s) drawn on it, sent
    # as a 2nd image (role REGION_MARK). Omit → no region path.
    regionAnnotation: ReferenceImage | None = None
    # Caller model/temperature override (group `edit-object`). Omit → DEFAULT
    # (model = seed row `prompt_templates.model`, temperature=GEMINI_TEMPERATURE).
    modelParams: EditObjectModelParams | None = None
    # AI-usage attribution — DUAL context (EditImageModal mounts both book AND remix).
    # Router stamps ONLY the winner: `remixId` present ⇒ remix cost (discriminator,
    # snapshot_id left NULL), else `snapshotId` ⇒ book cost.
    snapshotId: SnapshotId | None = None
    remixId: RemixId | None = None
    # Opt-in auto-persist (save-generated-resource util). Absent → no-op.
    saveResource: SaveResourceDirective | None = None

    @field_validator("prompt")
    @classmethod
    def _strip_and_validate_prompt(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("prompt must not be empty")
        if len(stripped) > MAX_PROMPT_LENGTH:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_LENGTH} chars")
        return stripped


class EditObjectImageData(BaseModel):
    imageUrl: str
    storagePath: str
    # AI-usage contract: `ai_service_logs.id` of the Gemini image-edit call.
    aiRequestId: str | None = None
    # save-generated-resource outcome (present only when `saveResource` was sent).
    saved: bool | None = None
    snapshotId: str | None = None
    saveError: str | None = None


class EditObjectImageMeta(BaseModel):
    processingTime: int | None = None
    mimeType: str | None = None
    tokenUsage: int | None = None
    # Echo of the resolved Gemini DISPATCH id actually used (parity with scene).
    model: str | None = None


class EditObjectImageResponse(BaseModel):
    success: bool
    data: EditObjectImageData
    meta: EditObjectImageMeta | None = None
