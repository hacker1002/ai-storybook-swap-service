"""Shared router helpers (error-response builder, URL-host extractor).

Ported from image-api's `src/routers/_shared/` — but WITHOUT `verify_api_key`:
remix routes here authenticate via the editor-session Bearer dep
(`src.auth.editor_session.require_editor_session`) at the router group level, not
`X-API-Key`.
"""
