#!/bin/bash
# Shared helpers for the JOB test-scripts (P3b).
#
# Builds on _editor-common.sh — that file is the ONLY place the
# `Authorization: Bearer` header is set (via req/mint_token). We NEVER add
# a service api-key header here: copying an image-api script verbatim is the 401 bug.
#
# Adds the async-job vocabulary the enqueue/cancel scripts need:
#   - enqueue_and_capture_job_id METHOD PATH BODY [LABEL]  -> sets CAPTURED_JOB_ID
#   - poll_job JOB_ID                                        -> loop until terminal
#   - job_field JOB_ID FIELD                                 -> single status field
#
# Polling talks to the P3a route  GET /api/jobs/status?ids=<id>  which returns
#   { success, data: { jobs: [ { id, type, status, step_details, result, ... } ],
#                      missing: [...] } }
# Terminal states: completed | failed | cancelled.

source "$(dirname "${BASH_SOURCE[0]}")/_editor-common.sh"

POLL="${POLL:-0}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-300}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-2}"

CAPTURED_JOB_ID=""

# assert_status_in "201 200" "RESPONSE_WITH_TRAILING_STATUS" LABEL
# Passes when the HTTP status is ANY of the space-separated expected list
# (enqueue routes legitimately return 201 enqueued / 200 skip|dedup).
assert_status_in() {
  local expected="$1" resp="$2" label="$3"
  local status; status="$(echo "$resp" | tail -1)"
  local bodyline; bodyline="$(echo "$resp" | sed '$d')"
  local e
  for e in $expected; do
    if [ "$status" = "$e" ]; then
      echo "  ✅ $label (HTTP $status)"
      return 0
    fi
  done
  echo "  ❌ $label — expected one of [$expected] got $status"
  echo "     body: $bodyline"
  FAILED=1
  return 1
}

# enqueue_and_capture_job_id METHOD PATH BODY [LABEL]
# Fires the request, prints HTTP status + body, and (on 2xx) extracts
# data.job_id into the global CAPTURED_JOB_ID. Returns the raw req output on
# stdout's LAST-line-is-status contract so callers can still assert_status.
enqueue_and_capture_job_id() {
  local method="$1" path="$2" body="$3" label="${4:-enqueue}"
  CAPTURED_JOB_ID=""
  # Diagnostics go to stderr so the function's stdout carries ONLY the raw req
  # output (body + trailing status) for the caller's assert_status_in.
  echo "  → $method $path" >&2
  echo "     payload: $body" >&2
  local resp; resp="$(req "$method" "$path" "$body")"
  local status; status="$(echo "$resp" | tail -1)"
  local bodyline; bodyline="$(echo "$resp" | sed '$d')"
  echo "     HTTP $status" >&2
  echo "     body: $bodyline" >&2
  CAPTURED_JOB_ID="$(printf '%s' "$bodyline" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
data = d.get("data") if isinstance(d, dict) else None
if isinstance(data, dict):
    print(data.get("job_id") or "")
' 2>/dev/null)"
  [ -n "$CAPTURED_JOB_ID" ] && echo "     job_id: $CAPTURED_JOB_ID" >&2
  # Emit ONLY the raw req output on stdout so the caller can assert_status on it.
  printf '%s' "$resp"
}

# job_field JOB_ID FIELD — prints jobs[0].<field> ("" if absent / missing[]).
job_field() {
  local job_id="$1" field="$2"
  local resp; resp="$(req GET "/api/jobs/status?ids=$job_id")"
  local bodyline; bodyline="$(echo "$resp" | sed '$d')"
  FIELD="$field" printf '%s' "$bodyline" | FIELD="$field" python3 -c '
import sys, json, os
field = os.environ["FIELD"]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
jobs = (d.get("data") or {}).get("jobs") or []
if jobs:
    v = jobs[0].get(field)
    if v is None:
        print("")
    elif isinstance(v, (dict, list)):
        print(json.dumps(v))
    else:
        print(v)
' 2>/dev/null
}

# poll_job JOB_ID — polls status until terminal or timeout. Prints status +
# step_details each loop. Sets global POLL_FINAL_STATUS. Returns 0 on
# completed, 1 on failed/timeout, 0 on cancelled (terminal, not an error here).
poll_job() {
  local job_id="$1"
  if [ -z "$job_id" ]; then
    echo "  ⚠️  poll_job: no job_id (skipped/dedup?) — nothing to poll."
    POLL_FINAL_STATUS="none"
    return 0
  fi
  echo "  ⏳ polling job_id=$job_id (timeout=${POLL_TIMEOUT_S}s interval=${POLL_INTERVAL_S}s)"
  local start; start="$(date +%s)"
  local status step
  while true; do
    status="$(job_field "$job_id" status)"
    step="$(job_field "$job_id" step_details)"
    [ -z "$status" ] && status="unknown"
    echo "     status=$status  step_details=${step:-<none>}  elapsed=$(( $(date +%s) - start ))s"
    case "$status" in
      completed)
        POLL_FINAL_STATUS="completed"; echo "  ✅ terminal: completed"; return 0 ;;
      failed)
        POLL_FINAL_STATUS="failed"; echo "  ❌ terminal: failed"; FAILED=1; return 1 ;;
      cancelled)
        POLL_FINAL_STATUS="cancelled"; echo "  ⚑ terminal: cancelled"; return 0 ;;
    esac
    if [ $(( $(date +%s) - start )) -ge "$POLL_TIMEOUT_S" ]; then
      echo "  ❌ poll timed out after ${POLL_TIMEOUT_S}s (last status=$status)"
      POLL_FINAL_STATUS="timeout"; FAILED=1; return 1
    fi
    sleep "$POLL_INTERVAL_S"
  done
}
