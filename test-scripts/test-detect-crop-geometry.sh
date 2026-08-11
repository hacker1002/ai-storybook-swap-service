#!/bin/bash
# Test: POST /api/remix/detect-crop-geometry  (2-step frame detect + classify)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 boxes (LIVE Gemini for a real run).
#
# Override fixtures:  ORIGINAL_SHEET_URL=... SWAPPED_SHEET_URL=...
source "$(dirname "$0")/_jobs-common.sh"
echo "== detect-crop-geometry =="

ORIGINAL_SHEET_URL="${ORIGINAL_SHEET_URL:-https://placedog.net/1024/768?id=1}"
SWAPPED_SHEET_URL="${SWAPPED_SHEET_URL:-https://placedog.net/1024/768?id=2}"

# Payload copied from image-api test-detect-crop-geometry.sh (2x3 grid).
BODY=$(cat <<JSON
{
  "original_sheet_url": "$ORIGINAL_SHEET_URL",
  "swapped_sheet_url": "$SWAPPED_SHEET_URL",
  "original_sheet_dimensions": { "width": 1464, "height": 912 },
  "crops": [
    { "number": 1, "geometry": { "x": 4,   "y": 64,  "w": 480, "h": 360 }, "recognition_hint": "Leela, base, Front view" },
    { "number": 2, "geometry": { "x": 492, "y": 64,  "w": 480, "h": 360 }, "recognition_hint": "Leela, casual, Side view" },
    { "number": 3, "geometry": { "x": 980, "y": 64,  "w": 480, "h": 360 }, "recognition_hint": "Kip, base, Back view" },
    { "number": 4, "geometry": { "x": 4,   "y": 488, "w": 480, "h": 360 }, "recognition_hint": "Kip, casual, Front view" },
    { "number": 5, "geometry": { "x": 492, "y": 488, "w": 480, "h": 360 }, "recognition_hint": "Mochi, base, Front view" },
    { "number": 6, "geometry": { "x": 980, "y": 488, "w": 480, "h": 360 }, "recognition_hint": "Mochi, casual, Side view" }
  ]
}
JSON
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/detect-crop-geometry" "$BODY")"; assert_status 200 "$r" "detect-crop-geometry 200"

finish
