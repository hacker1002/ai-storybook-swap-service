#!/usr/bin/env bash
# Build + deploy swap-service on the prod server (runs from repo root, via the
# self-hosted runner or by hand). Requires: docker + compose v2, secrets in STATE_DIR.
set -euo pipefail

STATE_DIR=/home/tbng84/Projects/AI-Story-Book/ai-storybook-swap-service
HEALTH_URL=http://localhost:3202/health
COMPOSE="docker compose -f deploy/compose.yml"
KEEP_TAGS=5

SHA=$(git rev-parse --short HEAD)
echo "==> deploying swap-service:$SHA"

# warn (not fail) on env keys present in .env.example but missing on the server
comm -23 <(grep -oE '^[A-Z_]+' .env.example | sort -u) \
         <(grep -oE '^[A-Z_]+' "$STATE_DIR/.env" | sort -u) \
  | sed 's/^/WARN missing in server .env: /' || true

PREV=$(docker inspect -f '{{.Config.Image}}' swap-service 2>/dev/null || echo "")
echo "==> current image: ${PREV:-<none>}"

docker build -t "swap-service:$SHA" .

TAG=$SHA $COMPOSE up -d

ok=""
for _ in $(seq 1 30); do
  sleep 2
  if curl -sf "$HEALTH_URL" >/dev/null; then
    ok=1
    break
  fi
done

if [ -z "$ok" ]; then
  echo "!! HEALTH GATE FAILED for swap-service:$SHA — recent logs:"
  journalctl CONTAINER_NAME=swap-service -n 100 --no-pager || true
  if [ -n "$PREV" ]; then
    echo "!! rolling back to $PREV"
    TAG=${PREV#swap-service:} $COMPOSE up -d
  else
    echo "!! no previous image to roll back to — container left as-is for inspection"
  fi
  exit 1
fi
echo "==> healthy: swap-service:$SHA"

# keep the last $KEEP_TAGS images for manual rollback, drop the rest
docker images swap-service --format '{{.Tag}}' \
  | grep -vx "$SHA" | tail -n "+$KEEP_TAGS" \
  | xargs -r -I{} docker rmi "swap-service:{}" 2>/dev/null || true
docker image prune -f >/dev/null

echo "==> deploy done: swap-service:$SHA"
