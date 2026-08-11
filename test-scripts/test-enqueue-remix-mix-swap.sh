#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/mix-swap  (ported job 05)
# Auth: Bearer — NEVER a service api-key header.
# Cases: 201/200 enqueue + optional poll; UNSUPPORTED_MODEL -> 422;
#        dedup on (remix_id) -> 409 (2nd back-to-back).
#
#   POLL=1  ./test-enqueue-remix-mix-swap.sh
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix mix-swap =="

REMIX_ID="${REMIX_ID:-}"
BATCH_ID="${BATCH_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

# Payload copied from image-api test-enqueue-remix-mix-swap.sh.
BODY="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false}"

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/mix-swap" "$BODY" "mix-swap")"
assert_status_in "201 200" "$r" "enqueue mix-swap"

# UNSUPPORTED_MODEL -> 422
BAD="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false,\"model_params\":{\"model\":\"bytedance/seedream-4.5\"}}"
r="$(req POST "/api/jobs/remix/$REMIX_ID/mix-swap" "$BAD")"; assert_status 422 "$r" "unsupported model -> 422"

# Dedup on (remix_id): fire twice back-to-back — 2nd must 409 (or 200 deduped).
req POST "/api/jobs/remix/$REMIX_ID/mix-swap" "$BODY" >/dev/null
r="$(req POST "/api/jobs/remix/$REMIX_ID/mix-swap" "$BODY")"
assert_status_in "409 200" "$r" "dedup (remix_id) 2nd enqueue"

if [ "$POLL" = "1" ]; then poll_job "$CAPTURED_JOB_ID"; fi

finish
