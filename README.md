# Remix Swap Service

Editor-session gateway for the **Remix Editor sub-app** (ADR-052). A deliberate
backend-layer fork of `ai-storybook-image-api` — it shares **no code**, only the
API contract. Stack: **Python 3.12 + uv + FastAPI + asyncpg** (no Supabase SDK,
no PostgREST). Runs on **port 8100** so it can run alongside image-api (8000).

## Scope (P3a — foundation + CRUD)

| Endpoint | Spec |
|---|---|
| `GET  /api/editor/book-bundle/{book_id}` | 01 — bootstrap read (book + full snapshot + artStyle + humans + voices) |
| `GET  /api/editor/remixes?snapshot_id=` | 02 — list |
| `GET  /api/editor/remixes/{id}` | 03 — get |
| `POST /api/editor/remixes` | 04 — create |
| `PATCH /api/editor/remixes/{id}/columns` | 05 — update writable columns |
| `DELETE /api/editor/remixes/{id}` | 06 — delete (idempotent, 409 if busy) |
| `GET  /api/jobs/status?ids=` | 07 — batch job-status polling |

## Auth

`Authorization: Bearer <editor-session JWT>` (HS256, `aud=remix-editor`,
`role=admin`). The service **only verifies** — mint/refresh/revoke belong to the
Admin App backend. Secret: `REMIX_EDITOR_TOKEN_SECRET` (distinct from Supabase JWT
/ player token secrets; comma-separated list for rotation). `/health` is ungated.

Dev token mint (the Admin App mint endpoint does not exist yet — P2):

```bash
uv run python scripts/mint_dev_editor_token.py            # valid admin token
uv run python scripts/mint_dev_editor_token.py --expired  # negative-path token
```

## Run

```bash
uv sync
cp .env.example .env          # fill APP_DB_URL + REMIX_EDITOR_TOKEN_SECRET
uv run uvicorn src.main:app --reload --port 8100
curl 'http://localhost:8100/health?db=1'
```

## Test

```bash
uv run pytest tests/ -q                # unit (fake adapter, no DB) — must be green FIRST

# Live integration — preconditions:
#   1. `uv sync` done (else `uvicorn` won't spawn on a fresh checkout)
#   2. server up on :8100 + local Postgres reachable
#   3. REMIX_EDITOR_TOKEN_SECRET matches the running server, and fixtures/local-ids.env
#      seeded with a real BOOK_ID + SNAPSHOT_ID (JOB_ID optional)
# An exported REMIX_EDITOR_TOKEN_SECRET overrides .env (pydantic env-var precedence),
# so force a match on both sides in one shot:
export REMIX_EDITOR_TOKEN_SECRET=dev-remix-editor-secret-change-me
uv run python -m uvicorn src.main:app --port 8100 &   # APP_DB_URL still read from .env

./test-scripts/run-all.sh              # all specs 00–07 in dependency order (create seeds REMIX_ID)
./test-scripts/test-auth-verify.sh     # or a single script standalone
```

## Database access

Direct asyncpg via the single `AppDbAdapter` seam (`src/db/`). Zero-DDL: reuses
`background_jobs` + `ai_service_logs` + `remixes`/`books`/`snapshots`/`humans`/
`voices` AS-IS. Service-written rows stash `source: "remix-swap-service"` into
existing JSONB (`background_jobs.params`, `ai_service_logs.request.audit`) so cost/
debug stays separable when the DB is shared with the editor.

Notes / gotchas:
- `remixes` has **no** `book_id` — bridge is `remixes.snapshot_id → snapshots.book_id`.
- `humans`/`voices` have **no** `book_id` — the editor loads them globally; the
  adapter keeps the `book_id` signature but ignores it (parity, not a bug).
- `background_jobs.user_id` is NOT NULL FK → `auth.users`; jobs (P3b) use
  `REMIX_SWAP_SERVICE_USER_ID` (a pre-existing service account row).
- Connections are acquired **per query** — never held across an AI call.

## Deployment TODO

- **Narrow DB role.** Dev uses the `postgres` superuser (bypasses RLS; all authz
  is app-layer). Create a limited role with table-scoped grants for shared/staging.
