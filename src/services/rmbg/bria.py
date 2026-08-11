"""Bria `bria/remove-background` adapter — the v1 default model.

Uses the `image` field (format: uri) which accepts both HTTPS URLs and data
URIs; the sibling `image_url` field is plain-string and Bria rejects data URIs
there (E006). `preserve_alpha` keeps soft alpha edges in the output.
"""

from __future__ import annotations

from src.services.rmbg.base import RemoveBgAdapter


class BriaRemoveBgAdapter(RemoveBgAdapter):
    model_id = "bria/remove-background"

    def build_payload(self, image_value: str, preserve_alpha: bool) -> dict:
        return {"image": image_value, "preserve_alpha": preserve_alpha}
