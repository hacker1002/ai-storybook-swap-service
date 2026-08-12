#!/bin/bash
# Auth matrix (spec 00) — 8 cases against a live server. Uses book-bundle as the probe route.
source "$(dirname "$0")/_editor-common.sh"
[ -z "$BOOK_ID" ] && BOOK_ID="00000000-0000-4000-8000-000000000000"
P="/api/editor/book-bundle/$BOOK_ID"

echo "== auth-verify =="
# 1 missing header
r="$(curl -s -w '\n%{http_code}' "$BASE_URL$P")"; assert_status 401 "$r" "missing header"; assert_error_code TOKEN_MISSING "$r" "missing header code"
# 2 malformed
r="$(req GET "$P" "" "not-a-jwt")"; assert_status 401 "$r" "malformed"; assert_error_code TOKEN_INVALID "$r" "malformed code"
# 3 wrong signature
r="$(req GET "$P" "" "$(mint_token --secret wrong-secret)")"; assert_status 401 "$r" "wrong signature"
# 4 wrong aud
r="$(req GET "$P" "" "$(mint_token --aud player)")"; assert_status 401 "$r" "wrong aud"; assert_error_code TOKEN_INVALID "$r" "wrong aud code"
# 5 alg none
r="$(req GET "$P" "" "$(mint_token --alg none)")"; assert_status 401 "$r" "alg none"
# 6 expired
r="$(req GET "$P" "" "$(mint_token --expired)")"; assert_status 401 "$r" "expired"; assert_error_code TOKEN_EXPIRED "$r" "expired code"
# 7 role viewer -> 403
r="$(req GET "$P" "" "$(mint_token --role viewer)")"; assert_status 403 "$r" "viewer role"; assert_error_code FORBIDDEN "$r" "viewer code"
# 8 valid (200 if book exists, else 404 — either proves auth passed)
r="$(req GET "$P")"; s="$(echo "$r" | tail -1)"
if [ "$s" = "200" ] || [ "$s" = "404" ]; then echo "  ✅ valid token passed auth (HTTP $s)"; else echo "  ❌ valid token — got $s"; FAILED=1; fi

# 9 revoked token -> 401 TOKEN_INVALID (denylist, spec 00 §5 — NOT a distinct code).
# Mint a token with a known sid, revoke that sid via S2S, then verify it is rejected.
REVOKE_SID="verify-revoked-$(date +%s)"
REVOKED_TOK="$(mint_token --sid "$REVOKE_SID")"
curl -s -X POST "$BASE_URL/internal/auth/revoke" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${INTERNAL_API_KEY:-dev-internal-key-change-me}" \
  -d "{\"sid\":\"$REVOKE_SID\"}" >/dev/null
r="$(req GET "$P" "" "$REVOKED_TOK")"; assert_status 401 "$r" "revoked token"; assert_error_code TOKEN_INVALID "$r" "revoked token code"
finish
