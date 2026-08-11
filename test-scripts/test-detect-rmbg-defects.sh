#!/bin/bash
# Test: POST /api/remix/detect-rmbg-defects  (REMOVE-BG defect localization)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 (defects:[] = ran OK, no defect).
#
# Override fixtures with real RGBA cut-out crops for a true alpha run:
#   ORIG_URL_1/2 (opaque BEFORE)  RESULT_URL_1/2 (RGBA AFTER)
source "$(dirname "$0")/_jobs-common.sh"
echo "== detect-rmbg-defects (sync) =="

ORIG_URL_1="${ORIG_URL_1:-https://placedog.net/512/768?id=11}"
ORIG_URL_2="${ORIG_URL_2:-https://placedog.net/512/768?id=12}"
RESULT_URL_1="${RESULT_URL_1:-https://placedog.net/512/768?id=21}"
RESULT_URL_2="${RESULT_URL_2:-https://placedog.net/512/768?id=22}"

# Payload copied from image-api test-detect-rmbg-defects.sh.
BODY=$(cat <<JSON
{
  "sheet_geometry": {"width": 1024, "height": 768},
  "crops": [
    {"id": "c1", "media_url": "$ORIG_URL_1", "geometry": {"x": 0, "y": 0, "w": 512, "h": 768}},
    {"id": "c2", "media_url": "$ORIG_URL_2", "geometry": {"x": 512, "y": 0, "w": 512, "h": 768}}
  ],
  "result_crops": [
    {"id": "c1", "media_url": "$RESULT_URL_1", "geometry": {"x": 0, "y": 0, "w": 512, "h": 768}},
    {"id": "c2", "media_url": "$RESULT_URL_2", "geometry": {"x": 512, "y": 0, "w": 512, "h": 768}}
  ],
  "severity_threshold": "low",
  "max_defects": 30
}
JSON
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/detect-rmbg-defects" "$BODY")"; assert_status 200 "$r" "detect-rmbg-defects 200"

finish
