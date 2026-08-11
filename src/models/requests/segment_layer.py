"""Constant-only slice of image-api's `requests/segment_layer.py` (P3b).

Only `SAM3_FIXED_INPUT` is needed by `services/replicate_client.py`.
"""

SAM3_FIXED_INPUT: dict = {
    "mask_only": True,
    "return_zip": False,
    "save_overlay": False,
}

__all__ = ["SAM3_FIXED_INPUT"]
