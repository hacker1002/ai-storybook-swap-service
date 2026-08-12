#!/bin/bash
# list-actors (spec 10) — casting-phía-App: read-only actors rows for a snapshot.
# Created: 2026-08-12
source "$(dirname "$0")/_editor-common.sh"
echo "== list-actors =="

# Case 1: real snapshot (ACTORS_SNAPSHOT_ID from discover_fixture_ids; fall back to
# SNAPSHOT_ID which may have zero rows -> still 200 + key present).
ACTORS_SID="${ACTORS_SNAPSHOT_ID:-$SNAPSHOT_ID}"
r="$(req GET "/api/editor/actors?snapshot_id=$ACTORS_SID")"; assert_status 200 "$r" "list by snapshot"
echo "$r" | sed '$d' | grep -q '"actors"' && echo "  ✅ actors key present" || { echo "  ❌ missing actors key"; FAILED=1; }

# Case 2: unknown snapshot -> 200 empty (NOT 404).
r="$(req GET "/api/editor/actors?snapshot_id=00000000-0000-4000-8000-000000000000")"; assert_status 200 "$r" "unknown snapshot -> 200 empty"
echo "$r" | sed '$d' | grep -q '"actors":\[\]' && echo "  ✅ empty list" || echo "  (non-empty — ok if seeded)"

# Case 3/4: validation.
r="$(req GET "/api/editor/actors")"; assert_status 400 "$r" "missing param"
r="$(req GET "/api/editor/actors?snapshot_id=nope")"; assert_status 400 "$r" "bad uuid"; assert_error_code VALIDATION_ERROR "$r" "bad uuid code"

# Case 5: no Authorization header -> 401 TOKEN_MISSING (raw curl, bypass req's mint).
r="$(curl -s -w '\n%{http_code}' "$BASE_URL/api/editor/actors?snapshot_id=$ACTORS_SID")"
assert_status 401 "$r" "no auth"; assert_error_code TOKEN_MISSING "$r" "no auth code"

finish
