"""Pydantic request models for the auth endpoints (exchange + revoke, spec 00)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The handoff assertion JWT. max_length caps a payload-bomb (a JWT this size is
    # already absurd); min_length rejects an empty string before we try to decode.
    code: str = Field(min_length=1, max_length=4096)


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admin_ref: str | None = None
    sid: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "RevokeRequest":
        if not (self.admin_ref or self.sid):
            raise ValueError("at least one of admin_ref or sid is required")
        return self
