# Remix Swap Service

Editor-session gateway for the **Remix Editor sub-app** (ADR-052). A deliberate
backend-layer fork of `ai-storybook-python-api` — it shares **no code**, only the
API contract. Stack: **Python 3.12 + uv + FastAPI + asyncpg** (no Supabase SDK,
no PostgREST). Runs on **port 8100** so it can run alongside image-api (8000).

## Scope

### P3a — Foundation + CRUD (complete)

| Endpoint | Spec |
|---|---|
| `GET  /api/editor/book-bundle/{book_id}` | 01 — bootstrap read (book + full snapshot + humans + voices; `artStyle` always null — App DB rev 2 clones no `art_styles`) |
| `GET  /api/editor/remixes?snapshot_id=` | 02 — list |
| `GET  /api/editor/actors?snapshot_id=` | 10 — list actors (casting resolve phía App; full rows, no pipeline filter) |
| `GET  /api/editor/remixes/{id}` | 03 — get |
| `POST /api/editor/remixes` | 04 — create |
| `PATCH /api/editor/remixes/{id}/columns` | 05 — update writable columns |
| `DELETE /api/editor/remixes/{id}` | 06 — delete (idempotent, 409 if busy) |
| `GET  /api/jobs/status?ids=` | 07 — batch job-status polling |

### P3b — Jobs pipeline + remix sync ops (shipped 2026-08-11)

**Jobs enqueue/cancel** (9 routes):
- `POST /api/jobs/remix/{id}/audio-swap` → enqueue audio job (14)
- `POST /api/jobs/remix/{id}/sprite-swap` → enqueue sprite job (15)
- `POST /api/jobs/remix/{id}/mix-swap` → enqueue mix job (**409 if dedup**)
- `POST /api/jobs/remix/{id}/rmbg` → enqueue rmbg job (dedup 200)
- `POST /api/jobs/remix/{id}/upscale` → enqueue upscale job (dedup 200)
- `POST /api/jobs/remix/{id}/detect-sprite-defects` → enqueue sprite defect detector (dedup 200)
- `POST /api/jobs/remix/{id}/detect-mix-defects` → enqueue mix defect detector (dedup 200)
- `POST /api/jobs/remix/{id}/detect-rmbg-defects` → enqueue rmbg defect detector (dedup 200)
- `POST /api/jobs/remix/{id}/cancel/{job_id}` → cancel any job (idempotent)

**Remix sync ops** (7 routes, internal/test, no FE consumer):
- `POST /api/remix/build-crop-sheet` → build actor crop sheet from variant frame
- `POST /api/remix/swap-sprite-sheet` → sprite swap via Gemini
- `POST /api/remix/swap-mix-crop-sheet` → mix crop swap via Gemini
- `POST /api/remix/detect-crop-geometry` → detect crop boxes for cut (Gemini flash + numpy)
- `POST /api/remix/detect-crop-defects` → detect sprite/mix/rmbg defects (Gemini flash)
- `POST /api/remix/detect-swap-defects` → detect swap result quality
- `POST /api/remix/detect-mix-defects` → detect mix quality

**Infrastructure** (all *-swap-service scoped):
- In-process jobs lib: `AsyncJobRunner` + `JobReaper` (stale job cleanup scoped to `source='remix-swap-service'`)
- Storage adapter: single `AppStorageAdapter` seam over bucket `storybook-assets`; env-presence switch (ADR-054) between the self-hosted storage service (httpx S2S → `:8200`) and legacy Supabase Storage REST (rollback)
- Global Replicate semaphore + version pins (prevent burst overload)
- AI call layer: Gemini via `gemini_ainvoke` (ADC-Vertex), Replicate SDK
- Cost attribution: `ai_service_logs` with `request.audit={admin_ref,sid,source:"remix-swap-service"}` + `remix_id` key; `user_id` always NULL (service account)

## Auth

`Authorization: Bearer <editor-session JWT>` (HS256, `aud=remix-editor`,
`role=admin`). Since **ADR-053** the service OWNS the session lifecycle — it
mints, verifies, and revokes:

- **`POST /api/editor/auth/exchange`** (public, no Bearer) — verifies a 60s handoff
  assertion (`aud=remix-editor-handoff`, signed by the Admin App with the SHARED
  `REMIX_EDITOR_HANDOFF_SECRET`), enforces one-time `jti` + a hard 60s TTL clamp +
  per-IP rate limit, and mints a **flat 12h** access token. No refresh token.
- **`POST /internal/auth/revoke`** (S2S `X-API-Key: INTERNAL_API_KEY`, fail-closed)
  — adds a `sid` or `admin_ref` to an in-memory denylist checked on every request.

Secrets: `REMIX_EDITOR_TOKEN_SECRET` (LOCAL-ONLY — mints/verifies access tokens;
comma-list for rotation, mint uses the newest), `REMIX_EDITOR_HANDOFF_SECRET`
(SHARED with the Admin App), `INTERNAL_API_KEY` (S2S revoke guard). `/health` +
`/api/editor/auth/exchange` are the only ungated editor-facing paths.

> **Single-process only.** `used_jti` + denylist + rate limiter are in-memory ⇒ the
> service MUST run `workers=1` (use `scripts/run-service.sh`). Restart clears the
> denylist (ADR-053 trade-off — App may re-push revokes).

Dev flow (the Admin App mint endpoint does not exist yet — P2):

```bash
# PRIMARY — mint a handoff assertion + print a ready-to-paste browser deeplink
uv run python scripts/mint_dev_handoff_url.py --book-id <BOOK_ID> [--remix-id <ID>]

# Test-harness only — forge access tokens the exchange endpoint CANNOT produce
uv run python scripts/mint_dev_editor_token.py --mode access --expired   # negative-path
uv run python scripts/mint_dev_editor_token.py --mode handoff            # raw assertion
```

## Deliberate Divergences from image-api

Unlike `ai-storybook-python-api` (which this is a fork of), the Remix Swap Service intentionally diverges:

- **Auth**: Bearer editor-session JWT (`aud=remix-editor`), NOT `X-API-Key` header
- **AI cost logging**: `ai_service_logs.user_id` always NULL; request audit nests into `request.audit={admin_ref,sid,source:"remix-swap-service"}` JSONB. Attribution key is `remix_id`, not `book_id`
- **Job reaper scoping**: `list_stale_jobs` filters to `source='remix-swap-service'` (shared `background_jobs` table with image-api — must not reclaim image-api's jobs without their finalize hooks)
- **Mix-swap dedup**: Returns **409 Conflict** (image-api returns 200 deduped); other detectors (detect-mix, detect-rmbg) return 409; sprite/audio/rmbg/upscale/detect-sprite → 200 deduped. FE sub-app must treat mix-swap 409 as a normal dedup response
- **Storage**: single `AppStorageAdapter` seam; env-presence switch (ADR-054) — httpx S2S to the self-hosted storage service (`/files/{bucket}/{key}` read shape) when `STORAGE_SERVICE_URL` is set, else legacy Supabase Storage REST (rollback). No `supabase-py` SDK either way. Optional `STORAGE_INTERNAL_READ_BASE_URL` (parity image-api) rewrites persisted `/files/` URLs public→loopback-nginx at fetch-time for server-side re-fetches (`storage/internal_read.py`, hooked in `services/http_fetch.py`)
- **LangSmith project**: `remix-swap-service` (separate from image-api)
- **Remix sync routes** (`/api/remix/*`): internal/test only; keep image-api `RemixDomainError` envelope (no FE consumer today)
- **Exchange response body** (`POST /api/editor/auth/exchange`): FLAT `{access_token, expires_in, admin_name?}` — the ONLY editor-facing endpoint that does NOT use the `{success,data}` envelope (spec 00 + FE auth module both specify flat). Error paths keep the `{success,error}` envelope.

## Run

```bash
uv sync
cp .env.example .env          # fill APP_DB_URL + REMIX_EDITOR_TOKEN_SECRET + REMIX_EDITOR_HANDOFF_SECRET [+ INTERNAL_API_KEY]
./scripts/run-service.sh      # canonical entry — pins --workers 1 (ADR-053, in-memory stores)
curl 'http://localhost:8100/health?db=1'
```

**Do NOT** run `uvicorn ... --workers N` (N>1) or set `WEB_CONCURRENCY`/`UVICORN_WORKERS`
> 1 — the denylist/jti/rate-limit stores are single-process (ADR-053). The boot guard
rejects the env-var path; `scripts/run-service.sh` is the supported command.

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

## Storage cutover — deploy / rollback (ADR-054)

The storage backend is chosen at boot by env presence (`build_storage_adapter`).
Startup logs the chosen backend: `storage_backend=storage_service|supabase_legacy`.

- **Deploy (single deploy — env + code together):** ship the code and set the 3 vars
  `STORAGE_SERVICE_URL` (LOOPBACK `127.0.0.1:8200`), `STORAGE_SERVICE_API_KEY`
  (`swap-service` key), `STORAGE_PUBLIC_BASE_URL` (public domain / nginx) in the SAME
  deploy → restart (`workers=1`) → confirm the log line `storage_backend=storage_service`
  → smoke one upload → verify the object on disk under the storage service's
  `STORAGE_ROOT/{bucket}/{key}`. Boot fails fast if the cluster is half-configured —
  that is intentional; re-check the 3 vars.
- **Rollback:** clear **ALL THREE** `STORAGE_SERVICE_*` vars (half = boot fail by
  design) → restart → back on Supabase (`APP_STORAGE_*`). ⚠️ Objects written to the
  storage service during the ON window do NOT migrate back, and URLs already persisted
  in JSONB keep pointing at `/files/...` — keep the storage service alive to serve reads.
- ⚠️ **Never point `STORAGE_SERVICE_URL` at the public domain** — nginx proxies READ
  only, so every write would 403. Loopback for writes, domain for reads.
- **Pending (separate one-shot, ADR-054 §6):** rewrite legacy Supabase URLs already in
  the DB to `/files/...`. Reads tolerate both until then.
