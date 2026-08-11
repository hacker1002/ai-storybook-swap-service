"""Service error taxonomy + FastAPI exception handlers.

Envelope for the NEW editor-facing endpoints (specs 00-07):
    { "success": false, "error": { "code", "message", "details"? } }

This is intentionally DIFFERENT from image-api's mixed `{error}` / `{detail.error}`
shapes. When P3b ports image-api endpoints verbatim they keep their own handler —
do not force one envelope over both.

HTTP code convention (inherited from image-api):
  - Pydantic body invalid              -> 400 VALIDATION_ERROR
  - precondition (referenced row gone) -> 422 (e.g. SNAPSHOT_NOT_FOUND)
  - primary resource not found         -> 404 NOT_FOUND
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.core.logging import get_logger

logger = get_logger("errors")


class ServiceError(Exception):
    """Domain error carrying a stable `code` + HTTP status. Handlers render it to
    the spec envelope. `message` is client-safe (never raw DB/stack detail)."""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = details


# Code -> default HTTP status (auth spec §5 + specs 01-07). Constructors below pin
# the status so call-sites read as intent, not numbers.
def token_missing(message: str = "Authorization header missing") -> ServiceError:
    return ServiceError("TOKEN_MISSING", 401, message)


def token_invalid(message: str = "Invalid token") -> ServiceError:
    return ServiceError("TOKEN_INVALID", 401, message)


def token_expired(message: str = "Token expired") -> ServiceError:
    return ServiceError("TOKEN_EXPIRED", 401, message)


def forbidden(message: str = "Forbidden") -> ServiceError:
    return ServiceError("FORBIDDEN", 403, message)


def validation_error(message: str, details: dict | None = None) -> ServiceError:
    return ServiceError("VALIDATION_ERROR", 400, message, details)


def column_not_writable(column: str) -> ServiceError:
    return ServiceError(
        "COLUMN_NOT_WRITABLE",
        400,
        f"Column '{column}' is not writable via this endpoint",
        {"column": column},
    )


def not_found(message: str = "Not found") -> ServiceError:
    return ServiceError("NOT_FOUND", 404, message)


def remix_busy(message: str = "Remix has an active job") -> ServiceError:
    return ServiceError("REMIX_BUSY", 409, message)


def snapshot_not_found(message: str = "Snapshot not found") -> ServiceError:
    return ServiceError("SNAPSHOT_NOT_FOUND", 422, message)


def _envelope(exc: ServiceError) -> dict:
    error: dict = {"code": exc.code, "message": exc.message}
    if exc.details:
        error["details"] = exc.details
    return {"success": False, "error": error}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        # 5xx = unexpected; log with context. 4xx = expected client error; quiet.
        if exc.http_status >= 500:
            logger.error("service_error", extra={"data": {"code": exc.code}})
        return JSONResponse(status_code=exc.http_status, content=_envelope(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic body/query errors -> 400 VALIDATION_ERROR (NOT FastAPI's default
        422 — that collides with our precondition 422 convention)."""
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "Validation error")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"{loc}: {msg}" if loc else msg,
                    "details": {
                        "fields": [
                            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg")}
                            for e in errors
                        ]
                    },
                },
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        """Last resort: log full trace server-side, return a static 500 (no leak)."""
        logger.error("internal_error", extra={"data": {"type": exc.__class__.__name__}}, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        )
