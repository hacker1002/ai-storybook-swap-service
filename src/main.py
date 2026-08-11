"""FastAPI application entry point for the Remix Swap Service (port 8100).

Boundary: NO Supabase SDK. DB access is asyncpg via `AppDbAdapter`. Editor-facing
endpoints (specs 00-07) use the `{success,error}` envelope; P3b's ported endpoints
will keep image-api's own envelope + their own handlers.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

import src.jobs.handlers  # noqa: F401 — side-effect: registers job handlers BEFORE app = FastAPI(...)
from src.config.settings import settings
from src.core.errors import register_exception_handlers
from src.core.logging import configure_logging, get_logger
from src.db.adapter import set_adapter
from src.db.pool import close_pool, create_pool, get_pool
from src.db.postgres_adapter import PostgresAppDbAdapter
from src.jobs import reaper_loop, wait_all
from src.jobs.config import SHUTDOWN_TIMEOUT_SEC
from src.routers.editor.router import router as editor_router
from src.routers.jobs.router import router as jobs_router
from src.routers.remix.error_handler import remix_domain_error_handler
from src.routers.remix.router import router as remix_router
from src.services.ai_usage.logger import drain as drain_ai_logs
from src.services.remix.errors import RemixDomainError
from src.storage.adapter import set_storage
from src.storage.supabase_rest import SupabaseRestStorage

configure_logging()
logger = get_logger("main")
access_logger = get_logger("access")

_SKIP_LOG_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}
_BODY_METHODS = {"POST", "PATCH", "PUT"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Open the asyncpg pool + wire the adapter on startup; close on shutdown."""
    logger.info("lifespan_startup")
    pool = await create_pool()
    set_adapter(PostgresAppDbAdapter(pool))
    # Storage seam: Supabase Storage REST over httpx (NO SDK). Construction is
    # I/O-free (URL/key only) — actual writes happen in P3b ported endpoints.
    set_storage(
        SupabaseRestStorage(
            base_url=settings.app_storage_url,
            service_key=settings.app_storage_service_key,
            default_bucket=settings.app_storage_bucket,
        )
    )
    # Startup guard: background_jobs.user_id is a NOT NULL FK -> auth.users. Verify
    # the configured service user exists before accepting job enqueues. Enforced
    # only when the value is set (unit tests leave it empty -> still boots).
    uid = settings.remix_swap_service_user_id
    if uid:
        try:
            uid_uuid = uuid.UUID(uid)
        except ValueError as exc:
            raise RuntimeError(f"REMIX_SWAP_SERVICE_USER_ID {uid!r} is not a valid UUID") from exc
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM auth.users WHERE id = $1", uid_uuid)
        if not exists:
            raise RuntimeError(
                f"REMIX_SWAP_SERVICE_USER_ID {uid!r} not found in auth.users — "
                "background_jobs.user_id is a NOT NULL FK; set it to a real auth.users id"
            )
    # Reaper reclaims stale running/queued jobs (config-constant thresholds).
    reaper_task = asyncio.create_task(reaper_loop(), name="reaper")
    try:
        yield
    finally:
        logger.info("lifespan_shutdown")
        reaper_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass
        await wait_all(timeout=SHUTDOWN_TIMEOUT_SEC)  # drain in-flight job handlers
        await drain_ai_logs()  # flush fire-and-forget ai_service_logs inserts
        await close_pool()  # LAST — wait_all/drain still write via the pool


app = FastAPI(
    title="Remix Swap Service",
    description="Editor-session gateway for the Remix Editor sub-app (ADR-052)",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Auth is Authorization: Bearer, not cookies -> credentials mode unneeded.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# GZip the large book-bundle (full snapshot JSONB). Only activates on
# Accept-Encoding: gzip, so plain curl/tests see uncompressed bodies.
app.add_middleware(GZipMiddleware, minimum_size=1024)

register_exception_handlers(app)


@app.middleware("http")
async def body_size_and_access_log(request: Request, call_next):
    """Reject over-cap bodies early via Content-Length (payload-bomb guard) + emit
    a correlated access log.

    NOTE: guards only requests that send Content-Length. A chunked
    (Transfer-Encoding: chunked, no length) upload bypasses this — rely on the
    upstream proxy / uvicorn `--limit-max-requests`/body limits for that case. The
    trusted sub-app client always sends Content-Length; hardening to a streaming
    read-cap is deferred (YAGNI at P3a)."""
    if request.method in _BODY_METHODS:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > settings.request_body_max_bytes:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "success": False,
                            "error": {"code": "VALIDATION_ERROR", "message": "Request body too large"},
                        },
                    )
            except ValueError:
                pass

    path = request.url.path
    if path in _SKIP_LOG_PATHS:
        return await call_next(request)

    req_id = uuid.uuid4().hex[:8]
    start = time.monotonic()
    access_logger.info("api_start", extra={"data": {"id": req_id, "method": request.method, "path": path}})
    response = await call_next(request)
    ms = int((time.monotonic() - start) * 1000)
    access_logger.info(
        "api_end",
        extra={"data": {"id": req_id, "method": request.method, "path": path, "status": response.status_code, "ms": ms}},
    )
    return response


app.include_router(editor_router)
app.include_router(jobs_router)
app.include_router(remix_router)
# Registered AFTER register_exception_handlers so this type-specific handler wins
# precedence over the catch-all Exception handler (remix routes keep image-api's
# own error envelope, NOT the /api/editor/* {success,error} shape).
app.add_exception_handler(RemixDomainError, remix_domain_error_handler)


@app.get("/health")
async def health_check(db: int = 0):
    """Liveness. `?db=1` runs a SELECT 1 to prove the DSN works (deep check)."""
    result = {"status": "healthy", "service": "remix-swap-service", "version": "0.1.0"}
    if db:
        from src.db.pool import get_pool

        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        result["db"] = "ok"
    return result
