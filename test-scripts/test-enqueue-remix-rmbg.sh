#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/rmbg  (ported job 09 — remove background)
# Auth: Bearer — NEVER a service api-key header.
# Cases: 201/200 enqueue + optional poll; UNSUPPORTED_MODEL -> 422.
#
#   POLL=1  ./test-enqueue-remix-rmbg.sh
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix rmbg =="

REMIX_ID="${REMIX_ID:-}"
BATCH_ID="${BATCH_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

# Payload copied from image-api test-enqueue-remix-rmbg.sh.
BODY="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false}"

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/rmbg" "$BODY" "rmbg")"
assert_status_in "201 200" "$r" "enqueue rmbg"

# UNSUPPORTED_MODEL (not in rmbg allowlist) -> 422
BAD="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false,\"model_params\":{\"model\":\"x/y\"}}"
r="$(req POST "/api/jobs/remix/$REMIX_ID/rmbg" "$BAD")"; assert_status 422 "$r" "unsupported model -> 422"

if [ "$POLL" = "1" ]; then poll_job "$CAPTURED_JOB_ID"; fi

finish
