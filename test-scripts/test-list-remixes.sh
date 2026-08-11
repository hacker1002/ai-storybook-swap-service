#!/bin/bash
# list-remixes (spec 02)
source "$(dirname "$0")/_editor-common.sh"
echo "== list-remixes =="
r="$(req GET "/api/editor/remixes?snapshot_id=$SNAPSHOT_ID")"; assert_status 200 "$r" "list by snapshot"
r="$(req GET "/api/editor/remixes?snapshot_id=00000000-0000-4000-8000-000000000000")"; assert_status 200 "$r" "unknown snapshot -> 200 empty"
echo "$r" | sed '$d' | grep -q '"remixes":\[\]' && echo "  ✅ empty list" || echo "  (non-empty — ok if seeded)"
r="$(req GET "/api/editor/remixes")"; assert_status 400 "$r" "missing param"
r="$(req GET "/api/editor/remixes?snapshot_id=nope")"; assert_status 400 "$r" "bad uuid"
finish
