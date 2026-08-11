"""Remix-router test app — mounts `/api/remix/*` on a STANDALONE FastAPI app.

The remix router is wired into the real `src/main.py` centrally (a parallel work
stream owns that file), so these tests build their own app to stay independent:
include the ported router + register the `RemixDomainError` handler exactly as the
main.py spec prescribes. Auth is the real editor-session Bearer dep — the token
comes from the top-level `auth_headers` fixture (conftest.py).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.errors import register_exception_handlers
from src.routers.remix.error_handler import remix_domain_error_handler
from src.routers.remix.router import router as remix_router
from src.services.remix.errors import RemixDomainError


@pytest.fixture
def remix_client() -> TestClient:
    app = FastAPI()
    # Editor envelope handlers (ServiceError / RequestValidationError / catch-all)
    # mirror the real app; the remix-specific handler is registered AFTER so its
    # type-specific dispatch wins over the catch-all `Exception` handler.
    register_exception_handlers(app)
    app.add_exception_handler(RemixDomainError, remix_domain_error_handler)
    app.include_router(remix_router)
    return TestClient(app)
