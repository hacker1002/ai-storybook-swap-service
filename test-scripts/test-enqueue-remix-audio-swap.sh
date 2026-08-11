#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/audio-swap  (ported job 01)
# Auth: Bearer (via _jobs-common.sh) — NEVER a service api-key header.
# Cases: 201 enqueue (or 200 skip) + optional poll; unknown remix -> 404.
#
#   POLL=1  ./test-enqueue-remix-audio-swap.sh   # enqueue then poll to terminal
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix audio-swap =="

REMIX_ID="${REMIX_ID:-}"
if [ -z "$REMIX_ID" ]; then echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"; fi

# Payload copied from image-api test-enqueue-remix-audio-swap.sh (jq builder).
BODY='{"triggeredBy":"test-script","maxConcurrentChunksPerTextbox":2}'

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/audio-swap" "$BODY" "audio-swap")"
assert_status_in "201 200" "$r" "enqueue audio-swap"

# Unknown remix -> 404 REMIX_NOT_FOUND
FAKE="00000000-0000-4000-8000-000000000000"
r="$(req POST "/api/jobs/remix/$FAKE/audio-swap" "$BODY")"
assert_status 404 "$r" "unknown remix -> 404"

# Lifecycle: poll the captured job to a terminal state.
if [ "$POLL" = "1" ]; then poll_job "$CAPTURED_JOB_ID"; fi

finish
