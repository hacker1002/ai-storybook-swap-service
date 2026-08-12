# Changelog — ai-storybook-swap-service

Nơi chính thức ghi **divergence nội bộ** so với image-api theo spec 08 (`ai-storybook-design/service/remix-swap-service/08-ported-endpoints.md`) §Quy trình chống drift rule 2: thay đổi nội bộ (model/prompt/thuật toán/seam) không bắt buộc sync ngược image-api nhưng PHẢI ghi ở đây. Contract-affecting changes vẫn phải sync spec cùng đợt (rule 1).

## Divergences đang có hiệu lực (vs image-api)

| Endpoint / seam | Divergence | Lý do |
|---|---|---|
| `POST /api/retouch/image-remove-bg` | `data.media_url` luôn `null` (key vẫn render — shape giữ) | Service không có content-addressed re-host; image-api populate trên passthrough path |
| `resource_persist` (mọi endpoint dùng save-generated-resource) | No-op parity seam — `saved`/`snapshotId`/`saveError` render null; output URLs vẫn trả bình thường | App DB không có snapshot-write path của editor; FE sub-app tự ghi qua remixes CRUD |
| AI logging (`replicate`/`invoke`) | URL-as-`output_blobs`, KHÔNG re-host output files; `output_files=()`. (Row `id` client-mint đã KHÔI PHỤC parity 260812 — hết divergence phần id) | Không có content_store trong service |
| 400 `VALIDATION_ERROR` body | Diagnostic nesting `error.details.fields[]` (chỉ `loc`/`msg`) thay vì `error.fields[]` (có `type`) của image-api | `status`/`code`/`message` parity; FE chỉ đọc code/message. Ghi nhận — align nếu FE bắt đầu parse fields |
| Auth missing/invalid credential | 401 (`TOKEN_MISSING`/`TOKEN_INVALID`/`TOKEN_EXPIRED`) thay vì 403 của X-API-Key image-api | Delta chốt spec 00/08 |
| Reaper | Scoped `params.source = 'remix-swap-service'` (image-api không scope) | Bảng `background_jobs` shared — không reap job của service khác |
| `GET /api/jobs/status` | Endpoint MỚI (image-api không có — editor dùng realtime); `params` projection strip `admin_ref`/`sid` | Spec 07; 429 RATE_LIMITED deferred (note trong spec 07) |
| `GET /api/editor/actors` | Endpoint MỚI editor-native (image-api không có) — read-only `actors` rows theo `snapshot_id`, no pipeline-completeness filter | Spec 10; casting resolve phía App (chốt 260812) — sub-app materialize client-side lúc create-remix |
| `POST /api/editor/auth/exchange` | Body 200 **PHẲNG** `{access_token, expires_in, admin_name?}` — endpoint editor-facing DUY NHẤT không bọc `{success,data}` envelope (error vẫn `{success,error}`) | Spec 00 + FE auth module đều viết phẳng; ADR-053 |

## 2026-08-12 — AI log row `id` client-mint (khôi phục parity image-api, đảo divergence P3b)

P3b chốt "DB mints `ai_service_logs.id`" — hệ quả ngầm: `rid = new_request_id()` mà gemini/replicate/upscale mint trước provider call và surface làm `ai_request_id`/`data.aiRequestId` trong envelope KHÔNG khớp row id nào trong DB (envelope id không tra ngược được — inconsistency chờ nổ khi debug/provenance). Khôi phục cơ chế image-api:

- `AiLogEntry` thêm field `id` (optional); `_entry_to_row` ghi `id` (malformed/absent → logger mint uuid4 fallback, DB default thành last-resort).
- `_AI_LOG_COLUMNS` + `"id"`; choke points wire: replicate `_log_replicate_call(ai_request_id=rid)` (12 call sites kể cả upscale_core), gemini `invoke` pass `id=rid` (2 entries), elevenlabs mint tại log time (LOG-ONLY, không envelope).
- `get_ai_log(ai_request_id)` (P3c) giờ resolve được id từ envelope. KHÔNG migration (column `id` sẵn có, `gen_random_uuid()` chỉ là default).
- Divergence table cập nhật: AI logging chỉ còn lệch phần KHÔNG re-host output files. Design sync: service README §6 + spec 08 bảng delta (REV 260812).

## 2026-08-12 — ElevenLabs choke point: bỏ `id=` khỏi AiLogEntry (fix audio-swap fail 100%)

Port miss P3b: `elevenlabs_client._log_elevenlabs_call` giữ nguyên call shape image-api `AiLogEntry(id=new_request_id(), ...)` trong khi `AiLogEntry` của service KHÔNG có field `id` (DB mints `ai_service_logs.id` — xem `services/ai_usage/logger.py`; replicate/gemini đã adapt đúng từ đầu). Mọi call ElevenLabs → `TypeError: unexpected keyword argument 'id'` ném TẠI choke point (trước cả fire-and-forget) → job audio-swap fail toàn bộ textbox ở stage narrate-script. Fix: bỏ `id=`, bỏ import `new_request_id` unused; regression test `tests/services/test_elevenlabs_logging.py` chạy construction thật (stub `log_ai_request`) — trước đây job tests mock cả client nên không bắt được.

## 2026-08-12 — `rmbgs`/`upscales` writable qua PATCH /columns (fix COLUMN_NOT_WRITABLE khi add batch)

Spec 05 chốt 260810 loại `rmbgs`/`upscales` khỏi allowlist ("job-only, FE không có nhu cầu") — SAI: audit call-sites đếm sót các site dùng dynamic key `[stage]` trong remix-store (`addStageBatch`/`removeStageBatch`/`importStageBatch`/`relayoutStageBatchSheets`/`takeFinalBack`/`reconcileFinalsAfterMutation`). FE own batch LIFECYCLE client-side cho cả 3 stage columns; main editor không lộ bug vì `SupabaseRemixGateway` ghi thẳng RLS không allowlist. Sub-app add batch tab remove-bg → 400 `COLUMN_NOT_WRITABLE`.

Fix: chuyển `rmbgs`/`upscales` vào `WRITABLE_REMIX_COLUMNS` (9 cột) — cùng dual-writer class với `mixes` (FE lifecycle + job results, race gated FE-side `useAnySwapRunning` + dedup). `JOB_ONLY_COLUMNS` giữ nguyên cho seam `update_remix_job_column` (hết disjoint với WRITABLE — by design). Create vẫn force `[]`. Spec 05 sync cùng đợt (REV 260812).

## 2026-08-12 — Storage REST shim: thêm `apikey` header (fix 400 Invalid Compact JWS)

`SupabaseRestStorage._auth_headers` chỉ gửi `Authorization: Bearer <key>`. Với key format mới `sb_secret_...` (không phải JWT), gateway parse Bearer như JWS → 400 `Invalid Compact JWS` trên mọi upload/sign/delete (sprite-swap job fail `swapped=0 failed=1`). Fix: gửi kèm header `apikey: <key>` (giống supabase-py — lý do image-api không dính). Legacy JWT key không bị ảnh hưởng. Live-verified upload 200 trên local Supabase.

## 2026-08-12 — Editor session lifecycle về swap service (ADR-053)

Service GIỜ SỞ HỮU session lifecycle (trước chỉ verify). Bỏ refresh token → **1 access token flat 12h**.

- **NEW `POST /api/editor/auth/exchange`** (public, no Bearer): verify handoff assertion (`aud=remix-editor-handoff`, secret SHARED `REMIX_EDITOR_HANDOFF_SECRET`) → one-time `jti` + hard clamp `exp-iat ≤ 60s(+margin)` + rate-limit per-IP → mint access token 12h. Body 200 **phẳng** (divergence trên). Mọi lỗi assertion → 401 `HANDOFF_INVALID` (không phân biệt hoá — anti-oracle). `Cache-Control: no-store`.
- **NEW `POST /internal/auth/revoke`** (S2S `X-API-Key: INTERNAL_API_KEY`, **fail-closed** — key rỗng ⇒ 401 + boot warning): ghi `sid`/`admin_ref` vào denylist in-memory. Idempotent. `≥1` field (thiếu cả 2 → 400 VALIDATION_ERROR).
- **Verify +bước 8 denylist** (SAU role): revoked → `TOKEN_INVALID` (không code riêng). Revoked viewer vẫn `FORBIDDEN` (thứ tự role-trước-denylist).
- **Ràng buộc SINGLE-PROCESS** (`used_jti` + denylist + rate-limit in-memory): `workers=1` bắt buộc — `scripts/run-service.sh` pin `--workers 1`; boot guard reject `WEB_CONCURRENCY`/`UVICORN_WORKERS` >1 (KHÔNG bắt được `--workers N` qua CLI). Restart mất denylist (chấp nhận — App re-push tuỳ chọn).
- **Env mới**: `REMIX_EDITOR_HANDOFF_SECRET` (required, shared App), `INTERNAL_API_KEY` (default rỗng, fail-closed), `EDITOR_ACCESS_TOKEN_TTL_SECONDS`=43200, `AUTH_EXCHANGE_RATE_LIMIT_PER_MIN`=20. `REMIX_EDITOR_TOKEN_SECRET` GIỜ local-only (mint+verify), mint dùng secret cuối (`[-1]`).
- **Dev CLI**: `scripts/mint_dev_handoff_url.py` (chính — in assertion + URL browser `#handoff=`); `mint_dev_editor_token.py` hạ cấp test-harness (`--mode handoff|access`).

## 2026-08-12 — sync design chốt 260812 (casting-phía-App + clone rev 2)

- **NEW `GET /api/editor/actors?snapshot_id=`** (spec 10) — read-only full `actors` rows, order `created_at ASC`, no pipeline-completeness filter (FE reads batch state to disable presets). Editor-native ⇒ liệt kê ở bảng Divergences cùng nhóm `/api/jobs/status`. Snapshot lạ → `[]` (200, không 404 — parity `list_remixes`).
- **`get_art_style` bỏ khỏi adapter surface** (thu hẹp `AppDbAdapter` §7). App DB rev 2 không clone `art_styles` và drop `books.artstyle_id` ⇒ `book-bundle` (spec 01) hardcode `artStyle: null` (key GIỮ — additive-only). Live-verified: bundle trả null kể cả khi book Editor-schema còn `artstyle_id` populated.
- Audit cột `books` bị drop (`format_id`/`era_id`/`location_id`/`sketchstyle_id`/`template_layout` + `sound`/`music` transform): **zero call site** trong `src/` — `get_book` đọc full row qua `.get()`, cột vắng → None. `ART_STYLE_SHEET` trong `reference_prompt_builder.py` GIỮ nguyên (parity port, refs qua request payload — không query `art_styles`).

## 2026-08-11 — review compliance fixes (post-P3c)

- **Mix-swap dedup REVERT về 200 `{deduped:true, active_swap_key}`** (parity image-api / spec jobs/05) — bỏ divergence 409 tạm của Phase-06. Detect-mix (12) / detect-rmbg (13) giữ 409 = parity image-api, KHÔNG phải divergence.
- 6 routes sync `/api/remix/*` stamp `admin_ref`/`sid` vào `AiCallContext` (spec 08 §Delta AI logging) — đồng nhất với retouch/upscale P3c.
- `/api/jobs/status`: strip `admin_ref`/`sid` khỏi `params` projection (không lộ session id qua polling).
- 5 job handlers `@register` chuyển sang constants `src/core/job_types.py` (SSOT rule).
- Audit label jobs domain thống nhất convention `jobs.<job_type>` / `jobs.cancel` (trước đó lẫn full-path style).
