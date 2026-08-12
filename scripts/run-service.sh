#!/bin/bash
# Canonical run command for the Remix Swap Service.
#
# workers=1 is MANDATORY (ADR-053): the editor-session denylist, one-time `used_jti`
# store, and per-IP exchange rate limiter are all in-memory + single-process. With
# N workers each holds an independent copy → a handoff assertion could be exchanged
# N times and a revoke would only reach one worker. Scaling out REQUIRES moving
# those stores to Redis/DB FIRST (see ADR-053 §Trade-off). The lifespan boot guard
# in src/main.py catches WEB_CONCURRENCY/UVICORN_WORKERS but NOT a raw --workers N,
# so THIS wrapper — not a hand-typed uvicorn line — is the supported entry point.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run uvicorn src.main:app --host 0.0.0.0 --port "${PORT:-8100}" --workers 1
