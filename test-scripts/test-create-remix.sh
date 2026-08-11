#!/bin/bash
# create-remix (spec 04). Prints the created remix id to fixtures for chaining.
source "$(dirname "$0")/_editor-common.sh"
echo "== create-remix =="
BODY="{\"snapshot_id\":\"$SNAPSHOT_ID\",\"name\":\"\",\"remix_config\":{\"v\":1},\"illustration\":{},\"characters\":[],\"rmbgs\":[{\"x\":1}]}"
r="$(req POST "/api/editor/remixes" "$BODY")"; assert_status 201 "$r" "create"
body="$(echo "$r" | sed '$d')"
echo "$body" | grep -q '"name":"New Remix"' && echo "  ✅ name defaulted" || { echo "  ❌ name not defaulted"; FAILED=1; }
echo "$body" | grep -q '"rmbgs":\[\]' && echo "  ✅ rmbgs forced []" || { echo "  ❌ rmbgs not []"; FAILED=1; }
NEW_ID="$(echo "$body" | grep -o '"id":"[0-9a-f-]*"' | head -1 | cut -d'"' -f4)"
echo "  created remix: $NEW_ID"
[ -n "$NEW_ID" ] && grep -q '^REMIX_ID=' "$FIXTURES" 2>/dev/null && sed -i '' "s/^REMIX_ID=.*/REMIX_ID=$NEW_ID/" "$FIXTURES"
# 422 unknown snapshot
BAD="{\"snapshot_id\":\"00000000-0000-4000-8000-000000000000\",\"name\":\"x\",\"remix_config\":{},\"illustration\":{},\"characters\":[]}"
r="$(req POST "/api/editor/remixes" "$BAD")"; assert_status 422 "$r" "unknown snapshot"; assert_error_code SNAPSHOT_NOT_FOUND "$r" "snapshot code"
# 400 missing required
r="$(req POST "/api/editor/remixes" "{\"snapshot_id\":\"$SNAPSHOT_ID\"}")"; assert_status 400 "$r" "missing required"
finish
