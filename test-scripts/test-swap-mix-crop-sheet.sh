#!/bin/bash
# Test: POST /api/remix/swap-mix-crop-sheet  (MULTI-TARGET Gemini swap)
# Auth: Bearer — NEVER a service api-key header.  Sync -> 200 (LIVE Gemini for a real run).
#
# rev6 contract: variant-sheet input, N<=10 swap_targets, NO unchanged_references.
source "$(dirname "$0")/_jobs-common.sh"
echo "== swap-mix-crop-sheet =="

# Minimal valid payload copied from image-api test-swap-mix-crop-sheet.sh base_payload.
BODY=$(cat <<'JSON'
{
  "sheet_geometry": {"width": 1024, "height": 768},
  "crops": [
    {"id":"c1","media_url":"https://example.com/a.png","geometry":{"x":0,"y":0,"w":100,"h":100}}
  ],
  "swap_targets": [
    {"key":"leela","reference_image_url":"https://example.com/leela-swap.png","target_base_image_url":"https://example.com/leela-base.png","object_context":{"name":"Leela","age":"7","appearance":{},"visual_description":""}},
    {"key":"didi","reference_image_url":"https://example.com/didi-swap.png","target_base_image_url":"https://example.com/didi-base.png","object_context":{"name":"Didi","age":"6","appearance":{},"visual_description":""}}
  ]
}
JSON
)
echo "  payload: $BODY"
r="$(req POST "/api/remix/swap-mix-crop-sheet" "$BODY")"; assert_status 200 "$r" "swap-mix-crop-sheet 200"

finish
