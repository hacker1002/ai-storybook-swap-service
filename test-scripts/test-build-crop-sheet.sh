#!/bin/bash
# Test: POST /api/remix/build-crop-sheet  (stateless crop-sheet composer)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 sheet (base64).
source "$(dirname "$0")/_jobs-common.sh"
echo "== build-crop-sheet =="

BUCKET="${BUCKET:-https://kiprvibenjkhvzekbkrw.supabase.co/storage/v1/object/public/storybook-assets}"

# Payload copied from image-api test-build-crop-sheet.sh (didi 2-crop case).
BODY=$(cat <<JSON
{
  "sheet_geometry": {"width": 720, "height": 1144},
  "response_format": "base64",
  "frame": {"draw_ordinals": true},
  "crops": [
    {"id":"didi-0","media_url":"$BUCKET/remove-bg-objects/1776505519874-1776505501410-177650-nobg.png","geometry":{"x":64,"y":64,"w":605,"h":608}},
    {"id":"didi-1","media_url":"$BUCKET/remove-bg-objects/1776756058809-1776756025348-177675-nobg.png","geometry":{"x":64,"y":736,"w":520,"h":392}}
  ]
}
JSON
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/build-crop-sheet" "$BODY")"; assert_status 200 "$r" "build-crop-sheet 200"

finish
