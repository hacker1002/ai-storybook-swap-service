#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/detect-sprite-defects  (ported job)
# Auth: Bearer — NEVER a service api-key header.
# Cases: 201/200 enqueue + optional poll -> result.defectsBySheet.
#
#   POLL=1  ./test-enqueue-remix-detect-sprite-defects.sh
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix detect-sprite-defects =="

REMIX_ID="${REMIX_ID:-}"
SPRITE_ID="${SPRITE_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

# Payload copied from image-api test-detect-sprite-defects.sh.
BODY="{\"sprite_id\":\"$SPRITE_ID\",\"severity_threshold\":\"low\",\"max_defects\":30}"

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/detect-sprite-defects" "$BODY" "detect-sprite-defects")"
assert_status_in "201 200" "$r" "enqueue detect-sprite-defects"

if [ "$POLL" = "1" ]; then
  poll_job "$CAPTURED_JOB_ID"
  [ "$POLL_FINAL_STATUS" = "completed" ] && echo "  result: $(job_field "$CAPTURED_JOB_ID" result)"
fi

finish
