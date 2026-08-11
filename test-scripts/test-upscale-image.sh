#!/bin/bash
# POST /api/image/upscale-image (P3c Phase 04 — Replicate multi-model upscaler)
source "$(dirname "$0")/_editor-common.sh"
echo "== upscale-image =="

SRC_URL="${UPSCALE_SRC_URL:-https://placedog.net/256/256?id=12}"

# auth gate
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/api/image/upscale-image" \
  -H 'Content-Type: application/json' -d '{"imageUrl":"'"$SRC_URL"'"}')"
assert_status 401 "$r" "no bearer -> 401"

# neither source -> 422 INVALID_IMAGE_SOURCE
r="$(req POST /api/image/upscale-image '{"scale":2}')"
assert_status 422 "$r" "no source -> 422"; assert_error_code INVALID_IMAGE_SOURCE "$r" "no source code"

# both sources -> 422 INVALID_IMAGE_SOURCE
r="$(req POST /api/image/upscale-image '{"imageUrl":"'"$SRC_URL"'","imageBase64":"AAAA"}')"
assert_status 422 "$r" "both sources -> 422"

# extra field (removed legacy `options`) -> 400
r="$(req POST /api/image/upscale-image '{"imageUrl":"'"$SRC_URL"'","options":{"x":1}}')"
assert_status 400 "$r" "extra field -> 400"

# scale out of range (>10) -> 400
r="$(req POST /api/image/upscale-image '{"imageUrl":"'"$SRC_URL"'","scale":99}')"
assert_status 400 "$r" "scale out of range -> 400"

# unsupported model -> 422
r="$(req POST /api/image/upscale-image '{"imageUrl":"'"$SRC_URL"'","modelParams":{"model":"bad/model"}}')"
assert_status 422 "$r" "bad model -> 422"; assert_error_code UNSUPPORTED_MODEL "$r" "bad model code"

if [ "${RUN_AI:-0}" = "1" ]; then
  echo "  -- RUN_AI=1: real Replicate upscale (slow, costs money) --"
  t0=$(date +%s)
  r="$(req POST /api/image/upscale-image '{"imageUrl":"'"$SRC_URL"'","scale":4}')"
  echo "  (took $(( $(date +%s) - t0 ))s)"
  assert_status 200 "$r" "real upscale -> 200"
else
  echo "  (skip AI happy-path — set RUN_AI=1 to exercise real Replicate)"
fi
finish
