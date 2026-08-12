#!/bin/bash
# POST /internal/auth/revoke (spec 00 §3) — S2S X-API-Key, writes in-memory denylist.
# Set INTERNAL_API_KEY LOCALLY here (never in _editor-common.sh — S2S is revoke-only).
source "$(dirname "$0")/_editor-common.sh"

INTERNAL_API_KEY="${INTERNAL_API_KEY:-dev-internal-key-change-me}"
RV="/internal/auth/revoke"
BID="${BOOK_ID:-00000000-0000-4000-8000-000000000000}"
PROBE="/api/editor/book-bundle/$BID"
echo "== auth-revoke =="

# revoke helper — S2S key + JSON body; print "body\nSTATUS"
revoke() {
  curl -s -w '\n%{http_code}' -X POST "$BASE_URL$RV" \
    -H "Content-Type: application/json" -H "X-API-Key: $INTERNAL_API_KEY" -d "$1"
}

# 1 revoke by sid -> that token then rejected 401 TOKEN_INVALID
SID1="revoke-sid-$(date +%s)-$RANDOM"
TOK1="$(mint_token --sid "$SID1")"
r="$(req GET "$PROBE" "" "$TOK1")"; s="$(echo "$r" | tail -1)"
if [ "$s" = "200" ] || [ "$s" = "404" ]; then echo "  ✅ token valid before revoke (HTTP $s)"; else echo "  ❌ pre-revoke — got $s"; FAILED=1; fi
r="$(revoke "{\"sid\":\"$SID1\"}")"; assert_status 200 "$r" "revoke by sid"
r="$(req GET "$PROBE" "" "$TOK1")"; assert_status 401 "$r" "post-revoke rejected"; assert_error_code TOKEN_INVALID "$r" "post-revoke code"

# 2 idempotent — revoke same sid again -> 200
r="$(revoke "{\"sid\":\"$SID1\"}")"; assert_status 200 "$r" "idempotent revoke"

# 3 revoke by admin_ref -> ANY sid of that admin rejected
AREF="revoke-admin-$(date +%s)-$RANDOM"
TOK_A="$(mint_token --admin-ref "$AREF" --sid "s-$RANDOM")"
r="$(revoke "{\"admin_ref\":\"$AREF\"}")"; assert_status 200 "$r" "revoke by admin_ref"
r="$(req GET "$PROBE" "" "$TOK_A")"; assert_status 401 "$r" "admin_ref sid rejected"; assert_error_code TOKEN_INVALID "$r" "admin_ref sid code"

# 4 missing S2S header -> 401
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL$RV" -H "Content-Type: application/json" -d '{"sid":"x"}')"
assert_status 401 "$r" "missing S2S header"

# 5 wrong S2S key -> 401
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL$RV" -H "Content-Type: application/json" -H "X-API-Key: totally-wrong" -d '{"sid":"x"}')"
assert_status 401 "$r" "wrong S2S key"

# 6 empty body {} (neither sid nor admin_ref) -> 400 VALIDATION_ERROR
r="$(revoke '{}')"; assert_status 400 "$r" "empty body"; assert_error_code VALIDATION_ERROR "$r" "empty body code"

finish
