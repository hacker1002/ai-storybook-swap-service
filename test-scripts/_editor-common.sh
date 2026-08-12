#!/bin/bash
# Shared helpers for Remix Swap Service test scripts.
# The ONLY place the Authorization: Bearer header is set (copying an image-api
# X-API-Key script and forgetting to switch is the classic bug — avoided here).

BASE_URL="${BASE_URL:-http://localhost:8100}"
export REMIX_EDITOR_TOKEN_SECRET="${REMIX_EDITOR_TOKEN_SECRET:-dev-remix-editor-secret-change-me}"
export REMIX_EDITOR_HANDOFF_SECRET="${REMIX_EDITOR_HANDOFF_SECRET:-dev-remix-handoff-secret-change-me}"
# NOTE: deliberately NOT setting any X-API-Key / S2S header here — the S2S guard is
# revoke-only and each S2S script sets INTERNAL_API_KEY locally (image-api copy bug).

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES="$_SCRIPT_DIR/fixtures/local-ids.env"
[ -f "$FIXTURES" ] && source "$FIXTURES"

# mint_token [extra flags...] -> prints an ACCESS token (harness — forged tokens)
mint_token() {
  (cd "$_SCRIPT_DIR/.." && uv run python scripts/mint_dev_editor_token.py "$@" 2>/dev/null)
}

# mint_handoff [extra flags...] -> prints a handoff assertion (exchange input)
mint_handoff() {
  (cd "$_SCRIPT_DIR/.." && uv run python scripts/mint_dev_editor_token.py --mode handoff "$@" 2>/dev/null)
}

# req METHOD PATH [json_body] [token]  -> prints "HTTP_STATUS\n<body>"
req() {
  local method="$1" path="$2" body="$3" token="$4"
  [ -z "$token" ] && token="$(mint_token)"
  local args=(-s -w '\n%{http_code}' -X "$method" "$BASE_URL$path" -H "Authorization: Bearer $token")
  if [ -n "$body" ]; then
    args+=(-H "Content-Type: application/json" -d "$body")
  fi
  curl "${args[@]}"
}

# assert_status EXPECTED "RESPONSE_WITH_TRAILING_STATUS" LABEL
assert_status() {
  local expected="$1" resp="$2" label="$3"
  local status; status="$(echo "$resp" | tail -1)"
  local bodyline; bodyline="$(echo "$resp" | sed '$d')"
  if [ "$status" = "$expected" ]; then
    echo "  ✅ $label (HTTP $status)"
    return 0
  else
    echo "  ❌ $label — expected $expected got $status"
    echo "     body: $bodyline"
    FAILED=1
    return 1
  fi
}

# assert_error_code EXPECTED_CODE "RESPONSE" LABEL
assert_error_code() {
  local expected="$1" resp="$2" label="$3"
  local bodyline; bodyline="$(echo "$resp" | sed '$d')"
  if echo "$bodyline" | grep -q "\"code\":\"$expected\""; then
    echo "  ✅ $label (code $expected)"
  else
    echo "  ❌ $label — expected code $expected"
    echo "     body: $bodyline"
    FAILED=1
  fi
}

FAILED=0
finish() { [ "$FAILED" = "0" ] && { echo "✅ PASSED"; exit 0; } || { echo "❌ FAILED"; exit 1; }; }
