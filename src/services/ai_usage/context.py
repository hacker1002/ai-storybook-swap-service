"""`AiCallContext` — the attribution payload threaded from a router/job-handler
down to a provider choke point (Gemini / Replicate / ElevenLabs).

Every field is optional. A choke point receives `ai_context=None` for callers that
carry no attribution — the row is still written, just with NULL attribution
columns. There is deliberately NO `operation` field here: `operation` is the call's
`run_name`, resolved at the choke point, not supplied by the caller.

`remix_id` is the cost DISCRIMINATOR: a row carrying it is billed to the remix
(view `ai_cost_by_remix`), never folded into the parent book.

DOCUMENTED BUG (image-api, MEMORY `AiCallContext ids must be str`): passing a raw
`UUID` into an id field silently drops the whole log row. `__post_init__` therefore
coerces every id to `str` defensively — the row-builder re-parses the uuid columns
back to `uuid.UUID` for asyncpg at the DB boundary (see `logger._entry_to_row`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every id-like field is coerced to `str` in `__post_init__`. `admin_ref`/`sid` are
# opaque refs that MAY arrive as UUIDs too, so they get the same defensive coercion.
_COERCE_FIELDS = (
    "book_id",
    "snapshot_id",
    "remix_id",
    "job_id",
    "user_id",
    "admin_ref",
    "sid",
)


@dataclass(frozen=True)
class AiCallContext:
    """Optional attribution for one AI call. All columns are SOFT FKs (nullable)."""

    book_id: str | None = None
    snapshot_id: str | None = None
    remix_id: str | None = None  # billing discriminator — remix cost, tracked separately
    job_id: str | None = None  # background_jobs.id when running inside a job handler
    user_id: str | None = None  # ALWAYS None in this service (App DB has no user directory)
    admin_ref: str | None = None  # opaque admin actor ref (NOT PII) → nested request.audit
    sid: str | None = None  # editor session id → nested request.audit

    # book_id resolution cache — a MUTABLE container. `frozen=True` blocks field
    # REASSIGNMENT, not mutation of the referenced dict, so the logger can resolve
    # `remix_id → book_id` ONCE and reuse it across every AI call sharing this ctx
    # (avoids one bridge query per call). Excluded from eq/hash/repr.
    _book_cache: dict = field(default_factory=dict, compare=False, repr=False, hash=False)

    def __post_init__(self) -> None:
        for name in _COERCE_FIELDS:
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                # frozen dataclass — bypass the write guard exactly like the stdlib
                # `dataclasses` does for default_factory fields.
                object.__setattr__(self, name, str(value))
