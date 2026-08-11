"""Shared Vertex AI client kwargs for every `ChatGoogleGenerativeAI` call-site.

Hard cutover (ADR-048): all Gemini calls run through Vertex AI (`location=global`)
with ADC auth — NO `google_api_key`, NO dual-mode fallback. `langchain-google-genai`
4.x selects the Vertex backend when `vertexai=True` is passed to the constructor
(the kwarg wins over any env passthrough), then falls back to Application Default
Credentials because no API key is supplied.

Every call-site spreads `**vertex_client_kwargs()` where it used to pass
`google_api_key=settings.google_api_key`. Keeping this a single pure function (no
side effects, no I/O) means the 17 constructors stay one-line diffs and the
`ChatGoogleGenerativeAI` symbol stays imported per-module (test seams unaffected).
"""

from __future__ import annotations

from src.config.settings import settings


def vertex_client_kwargs() -> dict:
    """Vertex AI backend + project selection for a `ChatGoogleGenerativeAI` ctor.

    Reads `settings` at call time (not import) so a test monkeypatching
    `settings.google_cloud_project` / `settings.vertex_ai_location` is honored.
    """
    return {
        "vertexai": True,
        "project": settings.google_cloud_project,
        "location": settings.vertex_ai_location,
    }
