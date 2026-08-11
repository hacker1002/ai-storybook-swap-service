#!/bin/bash
# book-bundle (spec 01)
source "$(dirname "$0")/_editor-common.sh"
echo "== get-book-bundle =="
r="$(req GET "/api/editor/book-bundle/$BOOK_ID")"; assert_status 200 "$r" "valid book"
echo "$r" | sed '$d' | grep -q '"contractVersion":1' && echo "  ✅ contractVersion present" || { echo "  ❌ contractVersion missing"; FAILED=1; }
for k in '"book"' '"snapshot"' '"artStyle"' '"humans"' '"voices"'; do
  echo "$r" | sed '$d' | grep -q "$k" && echo "  ✅ has $k" || { echo "  ❌ missing $k"; FAILED=1; }
done
r="$(req GET "/api/editor/book-bundle/00000000-0000-4000-8000-000000000000")"; assert_status 404 "$r" "unknown book"; assert_error_code NOT_FOUND "$r" "unknown book code"
r="$(req GET "/api/editor/book-bundle/not-a-uuid")"; assert_status 400 "$r" "bad uuid"
finish
