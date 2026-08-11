"""App-level exception handler for `RemixDomainError` (ported from image-api main.py).

Registered on the app in `src/main.py`:

    from src.services.remix.errors import RemixDomainError
    from src.routers.remix.error_handler import remix_domain_error_handler
    app.add_exception_handler(RemixDomainError, remix_domain_error_handler)

WHY a DEDICATED handler (not the editor `ServiceError` one): the ported `/api/remix/*`
routes keep image-api's OWN error envelope. It happens to share the flat
`{success, error:{code,message,details?}}` shape with this service's `ServiceError`
envelope, but the two error TYPES are kept separate so the remix contract never
depends on the editor taxonomy. Registering this type-specific handler also wins
type-precedence over the catch-all `@app.exception_handler(Exception)` in
`src/core/errors.py`, so a validator/service-raised `RemixDomainError` renders as the
spec envelope instead of a generic 500.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from src.services.remix.errors import RemixDomainError

__all__ = ["remix_domain_error_handler"]


async def remix_domain_error_handler(
    _request: Request, exc: RemixDomainError
) -> JSONResponse:
    """Surface `RemixDomainError` (raised inside Pydantic validators or a service
    core) as the spec envelope at top-level. Without this, validator-raised domain
    errors bubble through FastAPI's body parsing as a 500."""
    error: dict = {"code": exc.code, "message": exc.message}
    if exc.details:
        error["details"] = exc.details
    return JSONResponse(
        status_code=exc.status,
        content={"success": False, "error": error},
    )
