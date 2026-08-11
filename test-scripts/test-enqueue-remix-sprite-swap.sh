#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/sprite-swap  (ported job)
# Auth: Bearer — NEVER a service api-key header.
# Cases: 201/200 enqueue + optional poll; UNSUPPORTED_MODEL -> 422; dedup -> 409.
#
#   POLL=1  ./test-enqueue-remix-sprite-swap.sh
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix sprite-swap =="

REMIX_ID="${REMIX_ID:-}"
SPRITE_ID="${SPRITE_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

# Payload copied from image-api test-enqueue-remix-sprite-swap.sh.
BODY="{\"sprite_id\":\"$SPRITE_ID\",\"force_resweep\":false}"

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/sprite-swap" "$BODY" "sprite-swap")"
assert_status_in "201 200" "$r" "enqueue sprite-swap"

# UNSUPPORTED_MODEL -> 422
BAD="{\"sprite_id\":\"$SPRITE_ID\",\"force_resweep\":false,\"model_params\":{\"model\":\"openai/gpt-image-2\"}}"
r="$(req POST "/api/jobs/remix/$REMIX_ID/sprite-swap" "$BAD")"; assert_status 422 "$r" "unsupported model -> 422"

# Dedup: fire twice back-to-back (force_resweep) — 2nd must 409 (or 200 deduped).
D="{\"sprite_id\":\"$SPRITE_ID\",\"force_resweep\":true}"
req POST "/api/jobs/remix/$REMIX_ID/sprite-swap" "$D" >/dev/null
r="$(req POST "/api/jobs/remix/$REMIX_ID/sprite-swap" "$D")"
assert_status_in "409 200" "$r" "dedup 2nd enqueue"

if [ "$POLL" = "1" ]; then poll_job "$CAPTURED_JOB_ID"; fi

finish
