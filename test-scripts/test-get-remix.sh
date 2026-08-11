#!/bin/bash
# get-remix (spec 03)
source "$(dirname "$0")/_editor-common.sh"
echo "== get-remix =="
[ -z "$REMIX_ID" ] && { echo "  (no REMIX_ID — run test-create-remix.sh first)"; }
if [ -n "$REMIX_ID" ]; then
  r="$(req GET "/api/editor/remixes/$REMIX_ID")"; assert_status 200 "$r" "existing remix"
fi
r="$(req GET "/api/editor/remixes/00000000-0000-4000-8000-000000000000")"; assert_status 404 "$r" "unknown remix"
r="$(req GET "/api/editor/remixes/nope")"; assert_status 400 "$r" "bad uuid"
finish
