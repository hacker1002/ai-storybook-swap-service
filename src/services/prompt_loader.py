"""Generic prompt template loader — reusable for any text endpoint.

Parity with edge `_shared/prompt-builder.ts` (pattern: fetch system prompt →
extract skill marker → fetch skills → combine → render variables).

P3b PORT NOTE (Phase 02): the ONLY change vs image-api is the DB read — image-api
runs a blocking Supabase SDK query (`sb.table("prompt_templates").select(...).eq(
"name", name)`) wrapped in `asyncio.to_thread`; here it goes through the async
`AppDbAdapter.get_prompt_template(name)` seam (raw asyncpg, no SDK). The real key
column is **`name`** (verified against image-api's `_fetch_template_sync`). Model
still comes from `prompt_templates.model` with NO fallback for image-gen callers
(null → `PromptModelMissing` → 500 `PROMPT_TEMPLATE_NOT_FOUND`).
"""

import asyncio
import logging
import re

from src.db.adapter import get_adapter

__all__ = [
    "load_and_render",
    "fetch_template_row",
    "extract_skill_names",
    "render_variables",
    "PromptTemplateNotFound",
    "PromptModelMissing",
]


logger = logging.getLogger(__name__)

_SKILL_MARKER_REGEX = re.compile(r"@@Skill sử dụng:\s*([^@\n]+)")


class PromptTemplateNotFound(Exception):
    """Raised when a required prompt_templates row is missing."""


class PromptModelMissing(PromptTemplateNotFound):
    """Raised when `default_model=None` and the row carries no `model`.

    Subclass of `PromptTemplateNotFound` so existing `except PromptTemplateNotFound`
    branches map it to the same 500 `PROMPT_TEMPLATE_NOT_FOUND` envelope without
    handler churn. Used by image-gen callers (illustration) to FAIL FAST on a DB
    misconfig instead of silently falling back to the TEXT model `gemini-3.5-flash`
    (which 404s on image-gen). See plan Validation Session 1.
    """


def extract_skill_names(content: str) -> list[str]:
    match = _SKILL_MARKER_REGEX.search(content)
    if not match:
        return []
    return [s.strip() for s in match.group(1).split(",") if s.strip()]


def render_variables(template: str, variables: dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        pattern = re.compile(r"\{%request\." + re.escape(key) + r"%\}")
        rendered = pattern.sub(lambda _m, v=value: v, rendered)
    return rendered


async def _fetch_template(name: str) -> dict | None:
    """Fetch one `prompt_templates` row via the DB adapter (key column = `name`)."""
    return await get_adapter().get_prompt_template(name)


async def _fetch_templates_by_names(names: list[str]) -> list[dict]:
    """Fetch multiple `prompt_templates` rows (skills). Adapter exposes a single-row
    read, so fan out concurrently and drop the misses (parity with image-api's
    `.in_(names)` which silently returns only the rows that exist)."""
    if not names:
        return []
    rows = await asyncio.gather(*[_fetch_template(n) for n in names])
    return [r for r in rows if r is not None]


async def fetch_template_row(name: str) -> tuple[str, str | None]:
    """Fetch a `prompt_templates` row WITHOUT rendering — returns `(content, model)`.

    Fetch-only sibling of `load_and_render` for callers that need the row's
    `model` EARLY (fail-fast model resolve) and render LATER, after building a
    dynamic variable (e.g. edit-object resolves the dispatch model before it has
    the `reference_guide` to render). `load_and_render` fetches+renders in one
    call, so it can do neither. Reuses `_fetch_template`.

    The row's `model` is returned RAW (may be `None`) — the caller decides how a
    null model maps to an error (edit-object → 500 `PROMPT_TEMPLATE_NOT_FOUND`).

    Raises:
        PromptTemplateNotFound: if the `name` row is missing.
    """
    row = await _fetch_template(name)
    if row is None:
        raise PromptTemplateNotFound(f"prompt_templates.name={name!r}")
    return row["content"], row.get("model")


async def load_and_render(
    system_name: str,
    variables: dict[str, str],
    default_model: str | None = "gemini-3.5-flash",
) -> tuple[str, str]:
    """Load system prompt + referenced skills, combine, render variables.

    Args:
        system_name: `prompt_templates.name` of the system prompt (type=1).
        variables: map of variable key → rendered value (supports `{%request.<key>%}`).
        default_model: fallback when the row's `model` is null. Defaults to
            `"gemini-3.5-flash"` (backward-compat for text callers). Image-gen
            callers pass `None` to FAIL FAST (`PromptModelMissing`) on a DB row
            with no configured image model — a null there must NOT silently
            resolve to the text model.

    Returns:
        (rendered_prompt, model). `model` is the row's `model`, else `default_model`.

    Raises:
        PromptTemplateNotFound: if `system_name` row missing.
        PromptModelMissing: if the row has no `model` and `default_model is None`.
    """
    system_row = await _fetch_template(system_name)
    if system_row is None:
        raise PromptTemplateNotFound(f"prompt_templates.name={system_name!r}")

    system_content: str = system_row["content"]
    model: str | None = system_row.get("model") or default_model
    if not model:
        raise PromptModelMissing(
            f"prompt_templates.name={system_name!r} has no image model configured"
        )

    skill_names = extract_skill_names(system_content)
    skill_content = ""
    if skill_names:
        skill_rows = await _fetch_templates_by_names(skill_names)
        if len(skill_rows) < len(skill_names):
            missing = set(skill_names) - {r["name"] for r in skill_rows}
            logger.warning(
                "prompt_loader_skill_missing system=%s missing=%s",
                system_name, sorted(missing),
            )
        if skill_rows:
            # Preserve declared order
            by_name = {r["name"]: r["content"] for r in skill_rows}
            ordered = [by_name[n] for n in skill_names if n in by_name]
            skill_content = "\n\n".join(ordered)

    combined = ""
    if skill_content:
        combined += skill_content + "\n\n---\n\n"
    combined += system_content

    rendered = render_variables(combined, variables)
    return rendered, model
