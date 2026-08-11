#!/bin/bash
# POST /api/retouch/edit-object-image (P3c Phase 02 — Gemini image edit)
# Cheap paths run always; the AI happy-path (200, real Gemini + Storage) runs only
# with RUN_AI=1 (costs money + needs a reachable public source image).
source "$(dirname "$0")/_editor-common.sh"
echo "== edit-object-image =="

SRC_URL="${EDIT_SRC_URL:-https://placedog.net/800/600?id=10}"

# auth gate (no bearer)
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/api/retouch/edit-object-image" \
  -H 'Content-Type: application/json' -d '{"prompt":"x","imageUrl":"'"$SRC_URL"'"}')"
assert_status 401 "$r" "no bearer -> 401"

# missing required field (imageUrl) -> 400
r="$(req POST /api/retouch/edit-object-image '{"prompt":"add a bow"}')"
assert_status 400 "$r" "missing imageUrl -> 400"

# extra field (extra=forbid) -> 400
r="$(req POST /api/retouch/edit-object-image '{"prompt":"x","imageUrl":"'"$SRC_URL"'","bogus":1}')"
assert_status 400 "$r" "extra field -> 400"

# unsupported model -> 422 UNSUPPORTED_MODEL
r="$(req POST /api/retouch/edit-object-image '{"prompt":"x","imageUrl":"'"$SRC_URL"'","modelParams":{"model":"not-real"}}')"
assert_status 422 "$r" "bad model -> 422"; assert_error_code UNSUPPORTED_MODEL "$r" "bad model code"

# SSRF — private source URL -> 400 (SSRF guard on fetch)
r="$(req POST /api/retouch/edit-object-image '{"prompt":"x","imageUrl":"http://127.0.0.1:8100/x.png"}')"
assert_status 400 "$r" "private URL -> 400 SSRF"

if [ "${RUN_AI:-0}" = "1" ]; then
  echo "  -- RUN_AI=1: real Gemini edit (slow, costs money) --"
  t0=$(date +%s)
  r="$(req POST /api/retouch/edit-object-image '{"prompt":"add a small red bow","imageUrl":"'"$SRC_URL"'","aspectRatio":"1:1","imageSize":"1K"}')"
  echo "  (took $(( $(date +%s) - t0 ))s)"
  assert_status 200 "$r" "real edit -> 200"
else
  echo "  (skip AI happy-path — set RUN_AI=1 to exercise real Gemini)"
fi
finish
