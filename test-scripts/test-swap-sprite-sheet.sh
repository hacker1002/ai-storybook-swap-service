#!/bin/bash
# Test: POST /api/remix/swap-sprite-sheet  (per-object per-trait Gemini swap)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 (LIVE Gemini; ~25-45s + cost).
# A SAFETY_FILTER_BLOCKED (422) on a child-face human ref is an EXPECTED path.
#
# Override fixtures for a real run:  CROP_URL=... HUMAN_URL=...
source "$(dirname "$0")/_jobs-common.sh"
echo "== swap-sprite-sheet =="

CROP_URL="${CROP_URL:-https://placedog.net/512/768?id=1}"
CROP_URL_2="${CROP_URL_2:-https://placedog.net/512/768?id=2}"
HUMAN_URL="${HUMAN_URL:-https://i.pravatar.cc/512?img=12}"

# Payload copied from image-api test-swap-sprite-sheet.sh.
BODY=$(cat <<EOF
{
  "sheet_geometry": { "width": 1024, "height": 768 },
  "crops": [
    {"type":"character","object_key":"leela","variant_key":"base","media_url":"$CROP_URL","geometry":{"x":0,"y":0,"w":512,"h":768}},
    {"type":"character","object_key":"leela","variant_key":"casual","media_url":"$CROP_URL_2","geometry":{"x":512,"y":0,"w":512,"h":768}}
  ],
  "swap_objects": [
    {
      "object_key": "leela",
      "human_image_url": "$HUMAN_URL",
      "human_description": "young woman, warm brown eyes, shoulder-length dark hair",
      "swap_traits": [ { "type": "face", "description": "facial structure and skin tone" } ],
      "object_context": {
        "name": "Leela",
        "basic_info": { "gender": "female", "age": "7", "role": "protagonist" },
        "appearance": { "base_color": "#FFE4C4", "hair": "black long" },
        "visual_description": "animated girl with large expressive eyes"
      }
    }
  ],
  "return_composed_sheet": true
}
EOF
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/swap-sprite-sheet" "$BODY")"; assert_status 200 "$r" "swap-sprite-sheet 200"

finish
