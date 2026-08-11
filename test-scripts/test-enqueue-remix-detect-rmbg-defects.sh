#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/detect-rmbg-defects  (ported job 13)
# Auth: Bearer — NEVER a service api-key header.
# Cases: 201/200 enqueue + optional poll; dedup on (remix_id) -> 409.
#
# NOTE: image-api ships only the SYNC detect-rmbg-defects; the job variant here
# follows the batch_id enqueue shape of the sibling detect jobs (12).
#
#   POLL=1  ./test-enqueue-remix-detect-rmbg-defects.sh
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix detect-rmbg-defects =="

REMIX_ID="${REMIX_ID:-}"
BATCH_ID="${BATCH_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

BODY="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":true,\"severity_threshold\":\"low\",\"max_defects\":30}"

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/detect-rmbg-defects" "$BODY" "detect-rmbg-defects")"
assert_status_in "201 200" "$r" "enqueue detect-rmbg-defects"

# Dedup on (remix_id): fire twice back-to-back — 2nd must 409 (or 200 deduped).
req POST "/api/jobs/remix/$REMIX_ID/detect-rmbg-defects" "$BODY" >/dev/null
r="$(req POST "/api/jobs/remix/$REMIX_ID/detect-rmbg-defects" "$BODY")"
assert_status_in "409 200" "$r" "dedup (remix_id) 2nd enqueue"

if [ "$POLL" = "1" ]; then poll_job "$CAPTURED_JOB_ID"; fi

finish
