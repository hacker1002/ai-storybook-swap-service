#!/bin/bash
# POST /api/retouch/image-remove-bg (P3c Phase 03 — Replicate remove-bg)
source "$(dirname "$0")/_editor-common.sh"
echo "== image-remove-bg =="

SRC_URL="${RMBG_SRC_URL:-https://placedog.net/800/600?id=1}"

# auth gate
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/api/retouch/image-remove-bg" \
  -H 'Content-Type: application/json' -d '{"imageUrl":"'"$SRC_URL"'"}')"
assert_status 401 "$r" "no bearer -> 401"

# bad hex background color -> 400
r="$(req POST /api/retouch/image-remove-bg '{"imageUrl":"'"$SRC_URL"'","backgroundColor":"notahex"}')"
assert_status 400 "$r" "bad backgroundColor -> 400"

# unsupported model -> 422
r="$(req POST /api/retouch/image-remove-bg '{"imageUrl":"'"$SRC_URL"'","model":"totally/unknown"}')"
assert_status 422 "$r" "bad model -> 422"; assert_error_code UNSUPPORTED_MODEL "$r" "bad model code"

# SSRF private URL -> 400
r="$(req POST /api/retouch/image-remove-bg '{"imageUrl":"http://169.254.169.254/latest/meta-data"}')"
assert_status 400 "$r" "private URL -> 400 SSRF"

if [ "${RUN_AI:-0}" = "1" ]; then
  echo "  -- RUN_AI=1: real Replicate remove-bg (slow, costs money) --"
  t0=$(date +%s)
  r="$(req POST /api/retouch/image-remove-bg '{"imageUrl":"'"$SRC_URL"'","preserveAlpha":true}')"
  echo "  (took $(( $(date +%s) - t0 ))s)"
  assert_status 200 "$r" "real remove-bg -> 200"
else
  echo "  (skip AI happy-path — set RUN_AI=1 to exercise real Replicate)"
fi
finish
