FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# no apt deps: pillow/numpy install from wheels; no ffmpeg/libvips in this service

RUN useradd -r -u 999 -m app

ENV UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app
RUN chown app: /app
USER app

# deps layer — cached until pyproject/uv.lock change
COPY --chown=app:app pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/uv-cache,uid=999 uv sync --frozen --no-install-project --no-dev

COPY --chown=app:app . .
RUN --mount=type=cache,target=/tmp/uv-cache,uid=999 uv sync --frozen --no-dev

EXPOSE 3202

# 127.0.0.1: prod serves behind host nginx (network_mode: host); 3202 = prod port.
# --workers 1 MANDATORY (ADR-053): jti store / denylist / rate limiter are in-memory
# single-process; the boot guard only catches WEB_CONCURRENCY/UVICORN_WORKERS env vars.
CMD ["uv", "run", "--no-sync", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "3202", "--workers", "1"]
