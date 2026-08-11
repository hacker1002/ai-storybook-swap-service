"""Constant-only slice of image-api's `requests/normalize_human.py` (P3b).

Only `FACE_TO_MANY_MODEL` + `FACE_TO_MANY_VERSION` are needed by
`services/replicate_client.py` (community model → dispatch by pinned `version=`).
"""

FACE_TO_MANY_MODEL: str = "fofr/face-to-many"
FACE_TO_MANY_VERSION: str = (
    "a07f252abbbd832009640b27f063ea52d87d7a23a185ca165bec23b5adc8deaf"
)

__all__ = ["FACE_TO_MANY_MODEL", "FACE_TO_MANY_VERSION"]
