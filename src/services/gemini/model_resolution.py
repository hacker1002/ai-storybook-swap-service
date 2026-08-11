"""Unified Gemini model resolution (ADR-049).

Replaces ~7 hand-rolled per-endpoint resolvers that each re-implemented the SAME
precedence with subtly different code: `param (allowlist) > db_model (prompt_templates.model)
> fallback`. `resolve_gemini_model(param, db_model, group)` is the ONE precedence
implementation; a `group` picks the (allowlist, fallback) pair from `_GEMINI_GROUPS`.

TWO ids, never conflated (see `gemini_image_models`):
  - PUBLIC id (`google/nano-banana-pro`, `google/gemini-3.5-flash`) — caller-facing.
  - DISPATCH id (`gemini-3-pro-image`, `gemini-3.5-flash`) — what reaches the SDK.

`resolve_gemini_model` ALWAYS returns a dispatch id (or `None` for image-gen base
groups whose DB model is the only source → `None` lets the core fail-fast 500).

TWO DB conventions accepted with no data migration (`_normalize_db_model`):
  - image-gen / text rows store a BARE dispatch id (`gemini-3-pro-image`,
    `gemini-3.5-flash`) → used verbatim.
  - detect rows store a PUBLIC-prefixed id (`google/gemini-3.5-flash`) → mapped
    through the group allowlist to the bare dispatch id.
Canonical going-forward = public-prefixed (ADR-038 D1); verbatim bare stays valid.

GROUP GRANULARITY (Validation S1 Q4 + measured reality): text and detect-defects
are NOT one group each — the per-endpoint DB fallbacks genuinely DIVERGE
(`detect-mix-defects` falls back to `gemini-3-flash-preview`, not `gemini-3.5-flash`).
One group per distinct fallback keeps byte-parity; a group is one mapping line so
the granularity is free.

TEMPERATURE is resolved SEPARATELY (`clamp_temperature`) — model resolution and
temperature clamping are two concerns; the old `(model, temperature)` resolvers
conflated them.

P3b PORT NOTE (Phase 02): image-api sources every group's fallback constant from a
`src.models.requests.*` module. Those modules belong to endpoints OUTSIDE the
swap-service jobs pipeline, so the DB-fallback ids are re-declared LOCALLY here
(byte-identical values) instead of importing 12 out-of-scope request models. The
resolver LOGIC + the GEMINI_GROUPS table keys/values are unchanged, so
`resolve_gemini_model` yields the SAME dispatch id as image-api for any group.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from src.models.gemini_image_models import (
    GEMINI_IMAGE_MODEL_ID,
    PUBLIC_TO_GEMINI_IMAGE,
)
from src.services.remix.errors import RemixDomainError

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_gemini_model",
    "clamp_temperature",
    "GEMINI_GROUPS",
]

# ── Fallback constants re-declared verbatim from image-api's request modules ──
# (P3b: local block instead of `from src.models.requests.* import ...` — see the
# module docstring's P3b PORT NOTE. Values are byte-identical to image-api.)
DETECT_CROP_GEOMETRY_DEFAULT_MODEL: str = "gemini-3.5-flash"
DETECT_MIX_DEFECTS_DEFAULT_MODEL: str = "gemini-3-flash-preview"
DETECT_RMBG_DEFECTS_DEFAULT_MODEL: str = "gemini-3.5-flash"
DETECT_SWAP_DEFECTS_DEFAULT_MODEL: str = "gemini-3.5-flash"
DEFAULT_ENHANCE_ANNOTATION_MODEL: str = "gemini-3.5-flash"
DEFAULT_ENHANCE_NARRATION_MODEL: str = "gemini-3.5-flash"
DEFAULT_EXTRACT_TRAITS_MODEL: str = "gemini-3.5-flash"
DEFAULT_TRANSLATE_MODEL: str = "gemini-3.5-flash"
DETECT_OBJECTS_ALLOWLIST: dict[str, str] = {
    "google/gemini-3.5-flash": "gemini-3.5-flash",  # v1 default + only model
}
DETECT_DEFAULT_PUBLIC_MODEL: str = "google/gemini-3.5-flash"
DETECT_TEXTS_ALLOWLIST: dict[str, str] = {
    "google/gemini-3.5-flash": "gemini-3.5-flash",  # v1 default + only model
}
DETECT_TEXTS_DEFAULT_PUBLIC_MODEL: str = "google/gemini-3.5-flash"

_EMPTY_ALLOWLIST: dict[str, str] = {}

# group → (public→dispatch allowlist, fallback_dispatch_id | None).
#   - allowlist `{}`  → no param exposed (param path unreachable for the group).
#   - fallback `None` → DB model is the ONLY source; null DB → resolver returns
#     None so the core fails fast (illustration/sketch/scene base).
#
# CONSUMPTION (2026-07-22 / Đợt 1): the retouch trio (edit-object, generate-background,
# outpaint) + the 4 text groups call `resolve_gemini_model` today. The remaining groups
# (illustration-base, scene, sketch-*, detect-texts/-objects, detect-*-defects) are
# REGISTERED but not yet routed through here — those endpoints keep their own resolvers
# (`resolve_detect_model` w/ config-drift fallback; scene/sketch local resolvers) or
# `load_and_render(default_model=…)`. Their fallbacks reference the SAME constants (no
# drift). Kept in the table as the single fallback registry + the seam Đợt 2 (AI-request
# logging) will resolve every group through — do NOT assume those endpoints already flow
# through this resolver.
GEMINI_GROUPS: dict[str, tuple[Mapping[str, str], str | None]] = {
    # ── image-gen: share the app-wide PUBLIC_TO_GEMINI_IMAGE allowlist ──
    "illustration-base": (PUBLIC_TO_GEMINI_IMAGE, None),
    "scene": (PUBLIC_TO_GEMINI_IMAGE, None),
    "sketch-base": (PUBLIC_TO_GEMINI_IMAGE, None),
    "sketch-variant": (PUBLIC_TO_GEMINI_IMAGE, None),
    "sketch-spread": (PUBLIC_TO_GEMINI_IMAGE, None),
    "edit-object": (PUBLIC_TO_GEMINI_IMAGE, GEMINI_IMAGE_MODEL_ID),
    "generate-background": (PUBLIC_TO_GEMINI_IMAGE, GEMINI_IMAGE_MODEL_ID),
    "outpaint": (PUBLIC_TO_GEMINI_IMAGE, GEMINI_IMAGE_MODEL_ID),
    "parametric-variant": (PUBLIC_TO_GEMINI_IMAGE, GEMINI_IMAGE_MODEL_ID),
    # ── detect (param exposed via own allowlist; DB is public-prefixed) ──
    "detect-texts": (
        DETECT_TEXTS_ALLOWLIST,
        DETECT_TEXTS_ALLOWLIST[DETECT_TEXTS_DEFAULT_PUBLIC_MODEL],
    ),
    "detect-objects": (
        DETECT_OBJECTS_ALLOWLIST,
        DETECT_OBJECTS_ALLOWLIST[DETECT_DEFAULT_PUBLIC_MODEL],
    ),
    # ── text (no param; DB bare dispatch id; per-endpoint fallback) ──
    "translate": (_EMPTY_ALLOWLIST, DEFAULT_TRANSLATE_MODEL),
    "enhance-narration": (_EMPTY_ALLOWLIST, DEFAULT_ENHANCE_NARRATION_MODEL),
    "enhance-annotation": (_EMPTY_ALLOWLIST, DEFAULT_ENHANCE_ANNOTATION_MODEL),
    "extract-traits": (_EMPTY_ALLOWLIST, DEFAULT_EXTRACT_TRAITS_MODEL),
    # ── detect-defects (no param; DB bare dispatch id; fallbacks DIVERGE) ──
    "detect-crop-geometry": (_EMPTY_ALLOWLIST, DETECT_CROP_GEOMETRY_DEFAULT_MODEL),
    "detect-swap-defects": (_EMPTY_ALLOWLIST, DETECT_SWAP_DEFECTS_DEFAULT_MODEL),
    "detect-mix-defects": (_EMPTY_ALLOWLIST, DETECT_MIX_DEFECTS_DEFAULT_MODEL),
    "detect-rmbg-defects": (_EMPTY_ALLOWLIST, DETECT_RMBG_DEFECTS_DEFAULT_MODEL),
}


def _normalize_db_model(value: str, allowlist: Mapping[str, str]) -> str:
    """Map a `prompt_templates.model` value to a bare dispatch id.

    Accepts either DB convention: a public-prefixed id known to the group
    allowlist (`google/gemini-3.5-flash`) or the shared image allowlist maps to
    its dispatch id; anything else (a bare dispatch id — image-gen/text rows) is
    used verbatim. No data migration required.
    """
    if value in allowlist:
        return allowlist[value]
    if value in PUBLIC_TO_GEMINI_IMAGE:
        return PUBLIC_TO_GEMINI_IMAGE[value]
    return value


def resolve_gemini_model(
    param_public: str | None,
    db_model: str | None,
    group: str,
) -> str | None:
    """Resolve the effective BARE Gemini dispatch id for one request.

    Precedence: `param_public > db_model > group fallback`.
      1. `param_public` set → map through the group allowlist; out-of-allowlist →
         `RemixDomainError(422, UNSUPPORTED_MODEL, details={model})`. A raw model
         id is NEVER forwarded to the SDK (cost/abuse + injection guard).
      2. else `db_model` set → normalized to a dispatch id (both DB conventions).
      3. else group fallback (may be `None` → core fails fast on a null DB model).

    Raises `KeyError` for an unknown `group` (programmer error — the group set is
    closed + covered by tests).
    """
    allowlist, fallback = GEMINI_GROUPS[group]

    if param_public is not None:
        dispatch = allowlist.get(param_public)
        if dispatch is None:
            raise RemixDomainError(
                status=422,
                code="UNSUPPORTED_MODEL",
                message=f"model '{param_public}' not supported for group '{group}'",
                details={"model": param_public},
            )
        logger.debug(
            "gemini_model_resolved group=%s source=param public=%s dispatch=%s",
            group, param_public, dispatch,
        )
        return dispatch

    if db_model is not None:
        dispatch = _normalize_db_model(db_model, allowlist)
        logger.debug(
            "gemini_model_resolved group=%s source=db db=%s dispatch=%s",
            group, db_model, dispatch,
        )
        return dispatch

    logger.debug(
        "gemini_model_resolved group=%s source=fallback dispatch=%s", group, fallback
    )
    return fallback


def clamp_temperature(
    raw: float | None,
    default: float,
    lo: float = 0.0,
    hi: float = 2.0,
) -> float:
    """Return `raw` clamped to `[lo, hi]`, or `default` when `raw is None`.

    DRY replacement for the identical clamp the three retouch resolvers each
    inlined. Warns on an actual clamp so a caller sending an out-of-range value
    still sees it in the logs.
    """
    if raw is None:
        return default
    if raw < lo:
        logger.warning("temperature_clamped raw=%s lo=%s -> %s", raw, lo, lo)
        return lo
    if raw > hi:
        logger.warning("temperature_clamped raw=%s hi=%s -> %s", raw, hi, hi)
        return hi
    return raw
