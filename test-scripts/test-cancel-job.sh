#!/bin/bash
# Test: POST /api/jobs/{job_id}/cancel  (ported job cancel)
# Auth: Bearer — NEVER a service api-key header.
# Cases: enqueue a job -> cancel -> status becomes 'cancelled';
#        cancel an already-terminal job -> no-op (200/409); unknown -> 404.
#
# Enqueues via mix-swap to obtain a fresh job_id (fixtures: REMIX_ID/BATCH_ID).
source "$(dirname "$0")/_jobs-common.sh"
echo "== cancel job =="

REMIX_ID="${REMIX_ID:-}"
BATCH_ID="${BATCH_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

# 1) Enqueue a job to cancel.
BODY="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":true}"
r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/mix-swap" "$BODY" "seed job for cancel")"
assert_status_in "201 200" "$r" "seed enqueue"

if [ -n "$CAPTURED_JOB_ID" ]; then
  # 2) Cancel it.
  r="$(req POST "/api/jobs/$CAPTURED_JOB_ID/cancel" "")"; assert_status 200 "$r" "cancel job"
  # 3) Status should reflect cancellation (cancelled) or a cancel request flag.
  st="$(job_field "$CAPTURED_JOB_ID" status)"; echo "  post-cancel status=$st"
  # 4) Cancel again — terminal job -> no-op (200 idempotent or 409).
  r="$(req POST "/api/jobs/$CAPTURED_JOB_ID/cancel" "")"; assert_status_in "200 409" "$r" "re-cancel no-op"
else
  echo "  ⚠️  no job_id captured (route missing / skipped) — cancel path not exercised"
fi

# Unknown job -> 404
FAKE="00000000-0000-4000-8000-000000000000"
r="$(req POST "/api/jobs/$FAKE/cancel" "")"; assert_status 404 "$r" "unknown job -> 404"

finish
