#!/bin/bash
# update-remix-columns (spec 05) — also JSONB round-trip check
source "$(dirname "$0")/_editor-common.sh"
echo "== update-remix-columns =="
[ -z "$REMIX_ID" ] && { echo "  ❌ no REMIX_ID (run test-create-remix.sh first)"; FAILED=1; finish; }
r="$(req PATCH "/api/editor/remixes/$REMIX_ID/columns" '{"columns":{"mixes":[{"round":"trip"}],"name":"Patched"}}')"; assert_status 200 "$r" "writable columns"
# JSONB round-trip: read back
r="$(req GET "/api/editor/remixes/$REMIX_ID")"; echo "$r" | sed '$d' | grep -q '"round":"trip"' && echo "  ✅ JSONB round-trip" || { echo "  ❌ JSONB round-trip lost"; FAILED=1; }
r="$(req PATCH "/api/editor/remixes/$REMIX_ID/columns" '{"columns":{"remix_config":{}}}')"; assert_status 400 "$r" "remix_config rejected"; assert_error_code COLUMN_NOT_WRITABLE "$r" "remix_config code"
r="$(req PATCH "/api/editor/remixes/$REMIX_ID/columns" '{"columns":{"rmbgs":[]}}')"; assert_status 400 "$r" "rmbgs rejected"; assert_error_code COLUMN_NOT_WRITABLE "$r" "rmbgs code"
r="$(req PATCH "/api/editor/remixes/$REMIX_ID/columns" '{"columns":{}}')"; assert_status 400 "$r" "empty columns"
r="$(req PATCH "/api/editor/remixes/00000000-0000-4000-8000-000000000000/columns" '{"columns":{"name":"x"}}')"; assert_status 404 "$r" "unknown remix"
finish
