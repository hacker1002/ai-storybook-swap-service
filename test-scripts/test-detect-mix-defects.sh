#!/bin/bash
# Test: POST /api/remix/detect-mix-defects  (MIX swap defect localization)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 (defects:[] = ran OK, no defect).
#
# The image-api reference builds this payload from a real remix dump; here we use a
# self-contained multi-target shape (crops + result_crops + swap_targets).
# Override per-target URLs:  REF_URL=... BASE_URL_IMG=...
source "$(dirname "$0")/_jobs-common.sh"
echo "== detect-mix-defects (sync) =="

REF_URL="${REF_URL:-https://placedog.net/768/768?id=41}"
BASE_URL_IMG="${BASE_URL_IMG:-https://placedog.net/768/768?id=42}"
CROP_URL="${CROP_URL:-https://placedog.net/512/768?id=1}"
RESULT_URL="${RESULT_URL:-https://placedog.net/512/768?id=2}"

BODY=$(cat <<JSON
{
  "sheet_geometry": {"width": 1024, "height": 768},
  "crops": [
    {"id":"c1","media_url":"$CROP_URL","geometry":{"x":0,"y":0,"w":512,"h":768},"annotation":{"objects":["@leela, biến thể: base"]}},
    {"id":"c2","media_url":"$CROP_URL","geometry":{"x":512,"y":0,"w":512,"h":768},"annotation":{"objects":["@didi, biến thể: base"]}}
  ],
  "result_crops": [
    {"id":"c1","media_url":"$RESULT_URL","geometry":{"x":0,"y":0,"w":512,"h":768}},
    {"id":"c2","media_url":"$RESULT_URL","geometry":{"x":512,"y":0,"w":512,"h":768}}
  ],
  "swap_targets": [
    {"key":"leela","reference_image_url":"$REF_URL","target_base_image_url":"$BASE_URL_IMG","object_context":{"name":"Leela"}},
    {"key":"didi","reference_image_url":"$REF_URL","target_base_image_url":"$BASE_URL_IMG","object_context":{"name":"Didi"}}
  ],
  "swap_model": "google/nano-banana-pro",
  "swap_temperature": 0.25,
  "severity_threshold": "low",
  "max_defects": 30
}
JSON
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/detect-mix-defects" "$BODY")"; assert_status 200 "$r" "detect-mix-defects 200"

finish
