#!/bin/bash
# Test: POST /api/remix/detect-swap-defects  (swap defect localization)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 (defects:[] = ran OK, no defect).
#
# Override fixtures:  ORIG_URL=... SWAPPED_URL=... HUMAN_URL=...
source "$(dirname "$0")/_jobs-common.sh"
echo "== detect-swap-defects =="

ORIG_URL="${ORIG_URL:-https://placedog.net/1024/768?id=21}"
SWAPPED_URL="${SWAPPED_URL:-https://placedog.net/1024/768?id=22}"
HUMAN_URL="${HUMAN_URL:-https://i.pravatar.cc/512?img=12}"

# Payload copied from image-api test-detect-swap-defects.sh.
BODY=$(cat <<JSON
{
  "sheet_geometry": {"width": 1024, "height": 768},
  "crops": [
    {"type":"character","object_key":"leela","variant_key":"base","media_url":"$ORIG_URL","geometry":{"x":0,"y":0,"w":512,"h":768}},
    {"type":"character","object_key":"leela","variant_key":"casual","media_url":"$ORIG_URL","geometry":{"x":512,"y":0,"w":512,"h":768}}
  ],
  "result_crops": [
    {"type":"character","object_key":"leela","variant_key":"base","media_url":"$SWAPPED_URL","geometry":{"x":0,"y":0,"w":512,"h":768}},
    {"type":"character","object_key":"leela","variant_key":"casual","media_url":"$SWAPPED_URL","geometry":{"x":512,"y":0,"w":512,"h":768}}
  ],
  "swap_objects": [
    {
      "object_key": "leela",
      "human_image_url": "$HUMAN_URL",
      "human_description": "young girl with warm brown eyes",
      "swap_traits": [
        {"type": "face", "description": "facial structure and skin tone"},
        {"type": "hair", "description": "black long hair"}
      ],
      "object_context": {"name": "Leela", "appearance": {"hair": "black long"}, "visual_description": "animated girl"}
    }
  ],
  "swap_model": "google/nano-banana-pro",
  "swap_temperature": 0.25,
  "original_sheet_url": "$ORIG_URL",
  "severity_threshold": "low",
  "max_defects": 30
}
JSON
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/detect-swap-defects" "$BODY")"; assert_status 200 "$r" "detect-swap-defects 200"

finish
