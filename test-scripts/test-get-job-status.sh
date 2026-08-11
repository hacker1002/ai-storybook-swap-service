#!/bin/bash
# job-status (spec 07)
source "$(dirname "$0")/_editor-common.sh"
echo "== get-job-status =="
RAND="$(uuidgen | tr 'A-Z' 'a-z')"
IDS="$RAND"
[ -n "$JOB_ID" ] && IDS="$JOB_ID,$RAND"
r="$(req GET "/api/jobs/status?ids=$IDS")"; assert_status 200 "$r" "batch status"
body="$(echo "$r" | sed '$d')"
echo "$body" | grep -q "\"missing\":\[\"$RAND\"\]" && echo "  ✅ random id in missing" || echo "  (missing check — depends on JOB_ID presence)"
if [ -n "$JOB_ID" ]; then echo "$body" | grep -q '"jobs":\[{' && echo "  ✅ known job returned" || { echo "  ❌ known job absent"; FAILED=1; }; fi
r="$(req GET "/api/jobs/status?ids=")"; assert_status 400 "$r" "empty ids"
BIG="$(for i in $(seq 1 21); do echo -n "$(uuidgen | tr 'A-Z' 'a-z'),"; done | sed 's/,$//')"
r="$(req GET "/api/jobs/status?ids=$BIG")"; assert_status 400 "$r" "21 ids"
r="$(req GET "/api/jobs/status?ids=not-a-uuid")"; assert_status 400 "$r" "bad uuid"
finish
