"""`save_generated_resource` — opt-in generated-resource persist (NO-OP in this service).

Ported surface, deliberately DE-ACTIVATED. In image-api this orchestrates a write
of a just-generated resource back into the collab DB (Backend A snapshots / Backend
B remixes) per a client `saveResource` directive. In the Remix Swap Service the
remix routes are internal/test only (no FE consumer) and this service does NOT own
the collab-write machinery (remix_config is create-only, peer edits are discarded /
refetched — see project design). So the persist orchestration is intentionally a
no-op: `save_generated_resource(...)` always returns `None` (as if no directive was
sent), which keeps the endpoint's response byte-identical to image-api on the
NO-SAVE path (the only path any real caller exercises today).

`save_response_fields` is ported VERBATIM so the response `data` still carries the
additive `saved`/`snapshotId`/`saveError` keys (all `None` here), preserving the
wire contract. If a caller ever DOES send `saveResource`, it is accepted, ignored,
and reported as not-persisted (`saved: None`) rather than silently claiming success.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.services.resource_persist.models import (
    GeneratedResourceValue,
    PersistContext,
    SaveResourceDirective,
    SaveResourceOutcome,
)

logger = logging.getLogger(__name__)

__all__ = ["save_generated_resource", "save_response_fields"]


async def save_generated_resource(
    directive: Optional[SaveResourceDirective],
    value: GeneratedResourceValue,  # noqa: ARG001 — accepted for signature parity
    ctx: PersistContext,  # noqa: ARG001 — accepted for signature parity
) -> Optional[SaveResourceOutcome]:
    """No-op persist. Returns None (⇒ response fields read as not-applicable).

    A directive, if present, is logged + ignored (this service has no collab-write
    seam). NEVER raises — parity with image-api's soft-fail contract (persist errors
    must never fail the generate request)."""
    if directive is not None:
        logger.info(
            "save_generated_resource_noop type=%s root=%s (swap-service: persist disabled)",
            getattr(getattr(directive, "type", None), "value", "?"),
            (directive.path.split("/", 1)[0] if getattr(directive, "path", "") else ""),
        )
    return None


def save_response_fields(outcome: Optional[SaveResourceOutcome]) -> dict:
    """Additive response fields for the endpoint's `data` (camelCase wire names).

    Ported verbatim from image-api. `outcome is None` (the only case in this
    service) → all None ⇒ the fields read as absent/not-applicable (backward
    compatible — the client never asked). Splat into the response Data model:
    `Data(..., **save_response_fields(outcome))`."""
    if outcome is None:
        return {"saved": None, "snapshotId": None, "saveError": None}
    return {
        "saved": outcome.ok,
        "snapshotId": outcome.snapshot_id,
        "saveError": outcome.error if not outcome.ok else None,
    }
