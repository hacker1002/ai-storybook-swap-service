"""Constant-only slice of image-api's `requests/layering_image.py` (P3b).

Only `QWEN_LAYERED_MODEL` is needed by `services/replicate_client.py`.
"""

QWEN_LAYERED_MODEL: str = "qwen/qwen-image-layered"

__all__ = ["QWEN_LAYERED_MODEL"]
