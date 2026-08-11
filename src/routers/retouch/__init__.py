"""Retouch cores reused in-process by the remix job pipeline (P3b).

This package holds ONLY the framework-agnostic cores that a job handler calls
in-process (e.g. `image_remove_bg_core` for the `remix_rmbg` stage job). The public
`/api/retouch/*` HTTP endpoints are NOT ported into this service — it is a remix
sub-app gateway, not the retouch API.
"""
