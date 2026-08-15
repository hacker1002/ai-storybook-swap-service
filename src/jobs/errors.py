"""Structured handler error — appended to `result.errors[]` per spec 00 §3.6.

Ported verbatim from `ai-storybook-python-api/src/jobs/errors.py` (no DB, no
Supabase — pure dataclass). Kept identical so the job `result` envelope is
byte-for-byte compatible across the two services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

ErrorStage = Literal[
    "validate",
    "fetch",
    "process",
    "upload",
    "persist",
    "internal",
]


@dataclass
class JobError:
    stage: ErrorStage
    message: str
    spread_id: Optional[str] = None
    entity_key: Optional[str] = None
    code: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"stage": self.stage, "message": self.message}
        if self.spread_id is not None:
            d["spread_id"] = self.spread_id
        if self.entity_key is not None:
            d["entity_key"] = self.entity_key
        if self.code is not None:
            d["code"] = self.code
        return d
