#!/bin/bash
# Test: POST /api/jobs/remix/{remix_id}/upscale  (ported job 10)
# Auth: Bearer — NEVER a service api-key header.
# Cases: 201/200 enqueue + optional poll; model outside allowlist -> 422
#        UNSUPPORTED_MODEL; grain top-level knob accepted (201/200).
#
#   POLL=1  ./test-enqueue-remix-upscale.sh
source "$(dirname "$0")/_jobs-common.sh"
echo "== enqueue remix upscale =="

REMIX_ID="${REMIX_ID:-}"
BATCH_ID="${BATCH_ID:-00000000-0000-4000-8000-000000000000}"
[ -z "$REMIX_ID" ] && echo "  ⚠️  no REMIX_ID fixture — run scripts/seed_remix_fixture.py first"

# Payload copied from image-api test-enqueue-remix-upscale.sh.
BODY="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false}"

r="$(enqueue_and_capture_job_id POST "/api/jobs/remix/$REMIX_ID/upscale" "$BODY" "upscale")"
assert_status_in "201 200" "$r" "enqueue upscale"

# Model OUTSIDE the upscale allowlist -> 422 UNSUPPORTED_MODEL
BAD="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false,\"model_params\":{\"model\":\"openai/gpt-image-2\"}}"
r="$(req POST "/api/jobs/remix/$REMIX_ID/upscale" "$BAD")"; assert_status 422 "$r" "unsupported model -> 422"

# Grain (top-level body knob, model-agnostic) -> 201/200
G="{\"batch_id\":\"$BATCH_ID\",\"force_resweep\":false,\"grain\":{\"enabled\":true,\"amp\":9,\"blur\":0.8}}"
r="$(req POST "/api/jobs/remix/$REMIX_ID/upscale" "$G")"; assert_status_in "201 200" "$r" "grain enabled"

if [ "$POLL" = "1" ]; then poll_job "$CAPTURED_JOB_ID"; fi

finish
