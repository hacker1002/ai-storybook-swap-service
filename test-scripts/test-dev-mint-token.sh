#!/bin/bash
# Test script for: POST /api/dev/mint-editor-token (spec 10 — DEV-only mint stand-in)
# Created: 2026-08-11
#
# Precondition: server running with the dev mint FLAG ON, e.g.
#   export REMIX_EDITOR_TOKEN_SECRET=dev-remix-editor-secret-change-me
#   export DEV_MINT_ENABLED=true DEV_MINT_KEY=dev-mint-key-change-me
#   uv run python -m uvicorn src.main:app --port 8100 &
# Flag-OFF behavior (route absent -> plain 404) is by design untestable against a
# flag-on server — verify manually by restarting without DEV_MINT_ENABLED.
source "$(dirname "$0")/_editor-common.sh"
DEV_MINT_KEY="${DEV_MINT_KEY:-dev-mint-key-change-me}"
P="/api/dev/mint-editor-token"

# mint_req [json_body] [key] -> "body\nstatus" (no Bearer — this endpoint ISSUES tokens)
mint_req() {
  local body="$1" key="$2"
  local args=(-s -w '\n%{http_code}' -X POST "$BASE_URL$P")
  [ -n "$key" ] && args+=(-H "X-Dev-Mint-Key: $key")
  [ -n "$body" ] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}"
}

echo "== dev-mint-token =="

# 1 valid key, empty body -> 200 + token + no-store
r="$(mint_req "" "$DEV_MINT_KEY")"; assert_status 200 "$r" "mint (empty body)"
TOKEN="$(echo "$r" | sed '$d' | grep -o '"token":"[^"]*"' | cut -d'"' -f4)"
if [ -n "$TOKEN" ]; then echo "  ✅ token present"; else echo "  ❌ token missing"; FAILED=1; fi
CACHE="$(curl -s -o /dev/null -D - -X POST "$BASE_URL$P" -H "X-Dev-Mint-Key: $DEV_MINT_KEY" | grep -i '^cache-control')"
if echo "$CACHE" | grep -qi "no-store"; then echo "  ✅ Cache-Control: no-store"; else echo "  ❌ Cache-Control missing no-store ($CACHE)"; FAILED=1; fi

# 2 full-flow: minted token passes verify middleware on an editor route
[ -z "$BOOK_ID" ] && BOOK_ID="00000000-0000-4000-8000-000000000000"
r="$(req GET "/api/editor/book-bundle/$BOOK_ID" "" "$TOKEN")"; s="$(echo "$r" | tail -1)"
if [ "$s" = "200" ] || [ "$s" = "404" ]; then echo "  ✅ minted token passed auth (HTTP $s)"; else echo "  ❌ minted token rejected — got $s"; FAILED=1; fi

# 3 custom claims + ttl clamp (999999 -> 3600)
r="$(mint_req '{"adminRef":"qa-admin","sid":"qa-sid-1","ttlSeconds":999999}' "$DEV_MINT_KEY")"
assert_status 200 "$r" "mint (custom claims)"
bodyline="$(echo "$r" | sed '$d')"
echo "$bodyline" | grep -q '"adminRef":"qa-admin"' && echo "  ✅ adminRef echoed" || { echo "  ❌ adminRef not echoed"; FAILED=1; }
echo "$bodyline" | grep -q '"sid":"qa-sid-1"' && echo "  ✅ sid echoed" || { echo "  ❌ sid not echoed"; FAILED=1; }

# 4 wrong key -> 401 DEV_KEY_INVALID
r="$(mint_req "" "wrong-key")"; assert_status 401 "$r" "wrong key"; assert_error_code DEV_KEY_INVALID "$r" "wrong key code"

# 5 missing key -> 401 (indistinguishable from wrong)
r="$(mint_req "")"; assert_status 401 "$r" "missing key"; assert_error_code DEV_KEY_INVALID "$r" "missing key code"

# 6 bad body -> 400 VALIDATION_ERROR
r="$(mint_req '{"ttlSeconds":"abc"}' "$DEV_MINT_KEY")"; assert_status 400 "$r" "bad body"; assert_error_code VALIDATION_ERROR "$r" "bad body code"

# 7 extra field -> 400 (extra=forbid)
r="$(mint_req '{"bookId":"x"}' "$DEV_MINT_KEY")"; assert_status 400 "$r" "extra field forbidden"

finish
