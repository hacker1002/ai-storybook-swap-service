"""Models + custom exceptions for the `save_generated_resource` util.

This util is a CROSS-CUTTING helper (NOT an endpoint). It gives every "generate"
endpoint an opt-in `save_resource` directive to persist the just-generated
resource straight into the DB (snapshot node or a standalone table row), reusing
the collab gateway's `apply_snapshot_patch` RPC (Backend A) or a whole-blob
column/row write (Backend B).

Design pillars (spec `api/libs/save-generated-resource.md`):
  - **Client declares WHERE (`path`) + WHAT KIND (`type`); the endpoint injects
    the VALUE** (`media_url`/`ai_request_id`/…). The value is never client-supplied
    → no forged `media_url`.
  - `type ⊗ path` validation is the anti-"wrong-save" fence (see `type_registry`).
  - Absent directive ⇒ no-op ⇒ absolute backward compatibility.
  - Soft-fail: a save error NEVER 500s the endpoint (the resource is already in
    Storage) — the outcome carries an error code the client can retry on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ResourceType(str, Enum):
    """Semantic resource kind — selects the leaf-op (leaf array/obj + mutation).

    NOT mapped to the gateway's `rtype` (this util has no lock/authz/audit/sync,
    so `rtype` is irrelevant here). All version-prepend images collapse onto a
    single `IMAGE_VERSION` — the write grain (scene node vs entity vs remix
    whole-column) is derived from the `path`, never from the type.
    """

    IMAGE_VERSION = "image_version"
    TEXTBOX_AUDIO_CHUNK = "textbox_audio_chunk"
    TEXTBOX_COMBINED_AUDIO = "textbox_combined_audio"
    SPREAD_MEDIA = "spread_media"
    MUSIC_TRACK = "music_track"
    SOUND_EFFECT = "sound_effect"
    HUMAN_TRAITS = "human_traits"
    HUMAN_PROFILE_IMAGE = "human_profile_image"


# `action` → Illustration Entry `type`. Default (omitted) → 'created'.
SaveAction = Literal["create", "edit", "upload"]


class SaveResourceDirective(BaseModel):
    """The opt-in `saveResource` param added to every generate request model.

    Only `type` + `path` (+ optional `action`) come from the client; the RESOURCE
    VALUE (media_url, ai_request_id, …) is injected server-side by the endpoint.
    `extra="forbid"` so a client cannot smuggle a value field (e.g. `media_url`)
    into the directive.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    type: ResourceType
    path: str = Field(min_length=1)
    action: Optional[SaveAction] = None


@dataclass
class GeneratedResourceValue:
    """Server-injected value of the just-generated resource.

    The endpoint fills this from its OWN result (never the client): `media_url`
    is the durable Storage URL, `ai_request_id` the `ai_service_logs.id` minted
    before the AI call, `original_url` the pre-edit URL (edit tab only), and
    `extra` a bag for type-specific payloads (audio `results` item, music/sound
    row fields, spread-media container name, …).
    """

    media_url: str
    storage_path: Optional[str] = None
    ai_request_id: Optional[str] = None
    original_url: Optional[str] = None
    word_timings: Any = None
    duration: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PersistContext:
    """Trace context threaded into the util — mirrors `AiCallContext` MINUS the
    actor (this util writes service-role, never audits). `remix_id` is kept only
    for log attribution symmetry."""

    book_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    remix_id: Optional[str] = None


@dataclass
class SaveResourceOutcome:
    """Result of a save attempt. `snapshot_id` is populated only for Backend A
    (snapshot writes); Backend B (remix/music/sound/human) leaves it None."""

    ok: bool
    snapshot_id: Optional[str] = None
    error: Optional[str] = None


# ── custom exceptions (raised inside phase 01/02/03, caught by the orchestrator
#    → mapped to a soft-fail error code; NEVER propagate to the endpoint) ──


class SaveResourceError(Exception):
    """Base for every save-path failure. Carries a stable `code` (the value that
    lands in `SaveResourceOutcome.error` / `data.saveError`)."""

    code = "SAVE_RESOURCE_WRITE_FAILED"


class InvalidPath(SaveResourceError):
    """Path parse failure / table not in allowlist / column not in allowlist."""

    code = "SAVE_RESOURCE_INVALID_PATH"


class TypePathMismatch(SaveResourceError):
    """The `path` does not terminate at a container valid for `type` — the main
    anti-"wrong-save" fence."""

    code = "SAVE_RESOURCE_TYPE_PATH_MISMATCH"


class AnchorNotFound(SaveResourceError):
    """A `find:id=`/`find:key=`/`find:value=`/`idx:` locator matched no node."""

    code = "SAVE_RESOURCE_ANCHOR_NOT_FOUND"


class StaleSnapshot(SaveResourceError):
    """`snapshot_id` != `books.current_version` — writing into a frozen snapshot."""

    code = "STALE_SNAPSHOT_VERSION"
