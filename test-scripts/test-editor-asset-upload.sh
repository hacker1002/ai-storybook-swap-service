#!/bin/bash
# POST /api/editor/assets (P3c Phase 05 Gap 1 — proxy upload for erasor)
# No AI — always runs end-to-end (needs Storage reachable for the 201 happy path).
source "$(dirname "$0")/_editor-common.sh"
echo "== editor asset upload =="

PNG_B64="$(cat "$_SCRIPT_DIR/fixtures/tiny-png.b64")"

# auth gate
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/api/editor/assets" \
  -H 'Content-Type: application/json' -d '{"imageBase64":"'"$PNG_B64"'"}')"
assert_status 401 "$r" "no bearer -> 401"

# malformed base64 -> 400
r="$(req POST /api/editor/assets '{"imageBase64":"!!!not-base64!!!"}')"
assert_status 400 "$r" "malformed base64 -> 400"

# spoofed mime (text bytes) -> 400 (content sniff wins)
TXT_B64="$(printf 'this is not an image' | base64)"
r="$(req POST /api/editor/assets '{"imageBase64":"data:image/png;base64,'"$TXT_B64"'"}')"
assert_status 400 "$r" "spoofed mime -> 400"

# client-supplied path (extra=forbid) -> 400
r="$(req POST /api/editor/assets '{"imageBase64":"'"$PNG_B64"'","storagePath":"../evil.png"}')"
assert_status 400 "$r" "client path rejected -> 400"

# happy path -> 201 (real Storage upload)
r="$(req POST /api/editor/assets '{"imageBase64":"'"$PNG_B64"'"}')"
assert_status 201 "$r" "valid png -> 201"

finish
