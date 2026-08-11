"""App-level exception handler for `ImageDomainError` (ported from image-api main.py).

Registered on the app in `src/main.py`:

    from src.services.image.errors import ImageDomainError
    from src.routers.image.error_handler import image_domain_error_handler
    app.add_exception_handler(ImageDomainError, image_domain_error_handler)

WHY: the ported `/api/image/upscale-image` route surfaces failures as
`ImageDomainError` (INVALID_IMAGE_SOURCE from the body validator, INPUT_TOO_LARGE /
REPLICATE_ERROR etc. from `run_upscale`, and the UNSUPPORTED_MODEL remap). It shares
the flat `{success, error:{code,message,details?}}` envelope with `RemixDomainError`
but is a DISTINCT type, so it needs its own handler — and registering a type-specific
handler wins precedence over the catch-all `Exception` handler in `src/core/errors.py`
(without it a validator-raised ImageDomainError would render as a generic 500).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from src.services.image.errors import ImageDomainError

__all__ = ["image_domain_error_handler"]


async def image_domain_error_handler(
    _request: Request, exc: ImageDomainError
) -> JSONResponse:
    error: dict = {"code": exc.code, "message": exc.message}
    if exc.details:
        error["details"] = exc.details
    return JSONResponse(
        status_code=exc.status,
        content={"success": False, "error": error},
    )
