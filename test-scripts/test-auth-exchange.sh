#!/bin/bash
# POST /api/editor/auth/exchange (spec 00 §1) — handoff assertion -> access token 12h.
# The ONLY editor-facing endpoint with NO Authorization: Bearer (curl direct, not req()).
# Response body is FLAT { access_token, expires_in, admin_name? } (divergence: no
# {success,data} envelope — see CHANGELOG); errors keep the {success,error} envelope.
source "$(dirname "$0")/_editor-common.sh"

EX="/api/editor/auth/exchange"
echo "== auth-exchange =="

# helper: POST a raw {code} body (no Bearer), print "body\nSTATUS"
exchange() {
  curl -s -w '\n%{http_code}' -X POST "$BASE_URL$EX" \
    -H "Content-Type: application/json" -d "{\"code\":\"$1\"}"
}
# assert body contains a JSON key at root
assert_has() {
  local key="$1" resp="$2" label="$3"
  if echo "$resp" | sed '$d' | grep -q "\"$key\""; then echo "  ✅ $label (has $key)"; else
    echo "  ❌ $label — missing $key"; echo "     body: $(echo "$resp" | sed '$d')"; FAILED=1; fi
}

# 1 happy path — valid assertion -> 200 flat access_token, expires_in 43200
GOOD="$(mint_handoff)"
r="$(exchange "$GOOD")"; assert_status 200 "$r" "valid exchange"
assert_has access_token "$r" "valid exchange body"
if echo "$r" | sed '$d' | grep -q '"expires_in":43200'; then echo "  ✅ expires_in 43200"; else echo "  ❌ expires_in != 43200"; FAILED=1; fi
ACCESS_TOK="$(echo "$r" | sed '$d' | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"

# 2 replay — reuse the SAME assertion -> 401 HANDOFF_INVALID (one-time jti)
r="$(exchange "$GOOD")"; assert_status 401 "$r" "replay"; assert_error_code HANDOFF_INVALID "$r" "replay code"

# 3 wrong secret
r="$(exchange "$(mint_handoff --handoff-secret wrong-secret)")"; assert_status 401 "$r" "wrong secret"; assert_error_code HANDOFF_INVALID "$r" "wrong secret code"

# 4 expired
r="$(exchange "$(mint_handoff --expired)")"; assert_status 401 "$r" "expired"; assert_error_code HANDOFF_INVALID "$r" "expired code"

# 5 wrong aud (remix-editor instead of remix-editor-handoff)
r="$(exchange "$(mint_handoff --aud remix-editor)")"; assert_status 401 "$r" "wrong aud"; assert_error_code HANDOFF_INVALID "$r" "wrong aud code"

# 6 alg none
r="$(exchange "$(mint_handoff --alg none)")"; assert_status 401 "$r" "alg none"; assert_error_code HANDOFF_INVALID "$r" "alg none code"

# 7 missing code field -> 400 VALIDATION_ERROR
r="$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL$EX" -H "Content-Type: application/json" -d '{}')"
assert_status 400 "$r" "missing code"; assert_error_code VALIDATION_ERROR "$r" "missing code code"

# 8 admin_name echoed
r="$(exchange "$(mint_handoff --admin-name 'Nguyen A')")"; assert_status 200 "$r" "admin_name exchange"
assert_has admin_name "$r" "admin_name echoed"

# 9 minted access token passes editor auth (200 or 404 both prove auth passed)
if [ -n "$ACCESS_TOK" ]; then
  BID="${BOOK_ID:-00000000-0000-4000-8000-000000000000}"
  r="$(req GET "/api/editor/book-bundle/$BID" "" "$ACCESS_TOK")"; s="$(echo "$r" | tail -1)"
  if [ "$s" = "200" ] || [ "$s" = "404" ]; then echo "  ✅ exchanged token passes editor auth (HTTP $s)"; else echo "  ❌ exchanged token — got $s"; FAILED=1; fi
else
  echo "  ❌ could not extract access_token from case 1"; FAILED=1
fi

finish
