#!/bin/bash
# GET /api/provenance/ai-request-references/{id} (P3c Phase 05 Gap 2)
# No AI. Happy 200 needs a real ai_service_logs id (set PROV_ID); otherwise only
# the 404 / 400 / 401 paths are exercised.
source "$(dirname "$0")/_editor-common.sh"
echo "== provenance ai-request-references =="

# auth gate
r="$(curl -s -w '\n%{http_code}' "$BASE_URL/api/provenance/ai-request-references/11111111-1111-1111-1111-111111111111")"
assert_status 401 "$r" "no bearer -> 401"

# non-uuid path -> 400
r="$(req GET /api/provenance/ai-request-references/not-a-uuid)"
assert_status 400 "$r" "non-uuid -> 400"

# unknown id -> 404
r="$(req GET /api/provenance/ai-request-references/00000000-0000-4000-8000-000000000000)"
assert_status 404 "$r" "unknown id -> 404"

if [ -n "${PROV_ID:-}" ]; then
  echo "  -- PROV_ID set: real lookup --"
  r="$(req GET "/api/provenance/ai-request-references/$PROV_ID")"
  assert_status 200 "$r" "known id -> 200"
else
  echo "  (skip 200 path — set PROV_ID to a real ai_service_logs id)"
fi
finish
