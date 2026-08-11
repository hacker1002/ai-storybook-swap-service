"""Single source of truth for the Gemini image-generation model identity.

Every image-gen path that dispatches to `ChatGoogleGenerativeAI` shares ONE Gemini
model id + ONE public→Gemini id map, so a model bump is a one-line change here:
  - illustration scene override   (`requests/illustration.py::SCENE_MODEL_ALLOWLIST`)
  - retouch edit-object           (`requests/edit_object_image.py` — override allowlist
    via `PUBLIC_TO_GEMINI_IMAGE`; DEFAULT reads `prompt_templates.model`, not pinned here)
  - remix swap-mix / swap-sprite  (`services/remix/swap_*_sheet_core.py`)

Two distinct ids — never conflate them:
  - PUBLIC id (`google/nano-banana-pro`) — the caller-facing contract value (matches
    the swap model selector + generate-image-modal FE option). Callers send THIS.
  - DISPATCH id (`gemini-3-pro-image`) — what actually reaches the Gemini API.
    NEVER expose this raw provider id in a request contract.

Scope note: illustration base/variant read their model from `prompt_templates.model`
(DB, change-without-deploy) and are intentionally NOT pinned here. This module is the
hardcoded fallback/override identity shared by the endpoints that do NOT read the
model from the DB (+ the scene OVERRIDE allowlist, whose DEFAULT still comes from DB).
"""

# Gemini API model id dispatched to `ChatGoogleGenerativeAI(model=...)`.
GEMINI_IMAGE_MODEL_ID: str = "gemini-3-pro-image"

# Canonical PUBLIC model id exposed in request contracts (NOT a raw provider id).
PUBLIC_NANO_BANANA_PRO: str = "google/nano-banana-pro"

# Public id → Gemini dispatch id, shared by the scene allowlist + the swap cores'
# public→Gemini resolution. Add an entry only when a 2nd model is real + tested.
PUBLIC_TO_GEMINI_IMAGE: dict[str, str] = {
    PUBLIC_NANO_BANANA_PRO: GEMINI_IMAGE_MODEL_ID,
}

__all__ = [
    "GEMINI_IMAGE_MODEL_ID",
    "PUBLIC_NANO_BANANA_PRO",
    "PUBLIC_TO_GEMINI_IMAGE",
]
