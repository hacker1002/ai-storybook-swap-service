#!/bin/bash
# delete-remix (spec 06) — idempotent. Creates a throwaway remix so it can be deleted twice.
source "$(dirname "$0")/_editor-common.sh"
echo "== delete-remix =="
BODY="{\"snapshot_id\":\"$SNAPSHOT_ID\",\"name\":\"to-delete\",\"remix_config\":{},\"illustration\":{},\"characters\":[]}"
r="$(req POST "/api/editor/remixes" "$BODY")"; DID="$(echo "$r" | sed '$d' | grep -o '"id":"[0-9a-f-]*"' | head -1 | cut -d'"' -f4)"
echo "  throwaway remix: $DID"
r="$(req DELETE "/api/editor/remixes/$DID")"; assert_status 200 "$r" "first delete"; echo "$r" | sed '$d' | grep -q '"deleted":true' && echo "  ✅ deleted:true" || { echo "  ❌ not deleted:true"; FAILED=1; }
r="$(req DELETE "/api/editor/remixes/$DID")"; assert_status 200 "$r" "second delete idempotent"; echo "$r" | sed '$d' | grep -q '"deleted":false' && echo "  ✅ deleted:false" || { echo "  ❌ not deleted:false"; FAILED=1; }
r="$(req DELETE "/api/editor/remixes/nope")"; assert_status 400 "$r" "bad uuid"
finish
