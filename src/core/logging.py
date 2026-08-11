"""Structured JSON logging factory (Cloud-Logging friendly).

Level convention (mirrors docs/logging-convention.md): info = main flow entry/exit,
debug = branches, warn = unexpected fallbacks, error = failures WITH input context.
NEVER log secrets, tokens, base64/binary, or JSONB content (may hold book text/URLs).
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    """Render each record as one JSON line. `extra={"data": {...}}` is merged in
    flat under a `data` key; nothing else from the record dict leaks."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Install the JSON formatter on the root handler once (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    _CONFIGURED = True


def get_logger(module: str) -> logging.Logger:
    """Return a namespaced logger. Call `configure_logging()` at app startup first."""
    return logging.getLogger(f"remix-swap.{module}")
