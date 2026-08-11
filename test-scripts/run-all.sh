#!/bin/bash
# Runs every editor/jobs test-script in dependency order against a live server.
# Each child script calls `exit` via its own finish(), so we run them as
# subprocesses (NOT source) and aggregate exit codes. REMIX_ID is chained
# through fixtures/local-ids.env: create-remix writes it, later scripts read it.
#
# Usage:
#   BASE_URL=http://localhost:8100 ./test-scripts/run-all.sh
# Precondition: uvicorn up (port 8100) + local Postgres reachable, fixtures seeded
# with a real BOOK_ID + SNAPSHOT_ID (JOB_ID optional).

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Order matters: auth first, then the read probe, then create (seeds REMIX_ID),
# then everything that consumes REMIX_ID, then delete last.
SCRIPTS=(
  test-auth-verify.sh        # auth matrix (spec 00) — no state
  test-get-book-bundle.sh    # spec 01 — read only
  test-create-remix.sh       # spec 04 — WRITES REMIX_ID to fixtures
  test-list-remixes.sh       # spec 02
  test-get-remix.sh          # spec 03 — reads REMIX_ID
  test-update-remix-columns.sh # spec 05 — reads REMIX_ID
  test-get-job-status.sh     # spec 07
  test-delete-remix.sh       # spec 06 — creates its own throwaway, deletes twice
)

PASS=0; FAIL=0; FAILED_NAMES=()
echo "════════ Remix Swap Service — test-scripts (BASE_URL=${BASE_URL:-http://localhost:8100}) ════════"
for s in "${SCRIPTS[@]}"; do
  echo
  echo "──────── $s ────────"
  if bash "$DIR/$s"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_NAMES+=("$s")
  fi
done

echo
echo "════════ SUMMARY ════════"
echo "  passed: $PASS / $((PASS+FAIL))"
if [ "$FAIL" -ne 0 ]; then
  echo "  FAILED: ${FAILED_NAMES[*]}"
  exit 1
fi
echo "  ✅ ALL PASSED"
exit 0
