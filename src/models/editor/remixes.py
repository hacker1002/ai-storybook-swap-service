"""Pydantic request models for remixes CRUD (specs 04/05).

Validation is SHAPE-LIGHT on purpose: container type + required only. Deep-
validating the JSONB would re-implement FE logic and drift. `extra="allow"` keeps
create additive-only (unknown keys tolerated, but only allowlisted columns are
mapped into the INSERT).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateRemixPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    snapshot_id: str
    name: str = ""
    remix_config: dict[str, Any]
    illustration: dict[str, Any]
    characters: list[Any]
    props: list[Any] | None = None
    mixes: list[Any] | None = None
    sprites: list[Any] | None = None
    distribution: dict[str, Any] | None = None
    # rmbgs / upscales are job-only — server forces []; any client value is ignored.


class UpdateRemixColumnsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ≥1 entry (empty -> 400 VALIDATION_ERROR). Allowlist enforcement is in the
    # handler so a non-writable key yields 400 COLUMN_NOT_WRITABLE (not a generic
    # validation error).
    columns: dict[str, Any] = Field(min_length=1)
