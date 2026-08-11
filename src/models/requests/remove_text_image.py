"""Constant-only slice of image-api's `requests/remove_text_image.py` (P3b).

Only `TEXT_REMOVAL_DEFAULT_MODEL` is needed by `services/replicate_client.py`.
"""

TEXT_REMOVAL_DEFAULT_MODEL: str = "flux-kontext-apps/text-removal"

__all__ = ["TEXT_REMOVAL_DEFAULT_MODEL"]
