# Changelog — ai-storybook-swap-service

Nơi chính thức ghi **divergence nội bộ** so với image-api theo spec 08 (`ai-storybook-design/service/remix-swap-service/08-ported-endpoints.md`) §Quy trình chống drift rule 2: thay đổi nội bộ (model/prompt/thuật toán/seam) không bắt buộc sync ngược image-api nhưng PHẢI ghi ở đây. Contract-affecting changes vẫn phải sync spec cùng đợt (rule 1).

## Divergences đang có hiệu lực (vs image-api)

| Endpoint / seam | Divergence | Lý do |
|---|---|---|
| `POST /api/retouch/image-remove-bg` | `data.media_url` luôn `null` (key vẫn render — shape giữ) | Service không có content-addressed re-host; image-api populate trên passthrough path |
| `resource_persist` (mọi endpoint dùng save-generated-resource) | No-op parity seam — `saved`/`snapshotId`/`saveError` render null; output URLs vẫn trả bình thường | App DB không có snapshot-write path của editor; FE sub-app tự ghi qua remixes CRUD |
| AI logging (`replicate`/`invoke`) | Không có `ai_request_id` re-host + URL-as-`output_blobs`; `output_files=()` | Kế thừa P3b — không có content_store trong service |
| 400 `VALIDATION_ERROR` body | Diagnostic nesting `error.details.fields[]` (chỉ `loc`/`msg`) thay vì `error.fields[]` (có `type`) của image-api | `status`/`code`/`message` parity; FE chỉ đọc code/message. Ghi nhận — align nếu FE bắt đầu parse fields |
| Auth missing/invalid credential | 401 (`TOKEN_MISSING`/`TOKEN_INVALID`/`TOKEN_EXPIRED`) thay vì 403 của X-API-Key image-api | Delta chốt spec 00/08 |
| Reaper | Scoped `params.source = 'remix-swap-service'` (image-api không scope) | Bảng `background_jobs` shared — không reap job của service khác |
| `GET /api/jobs/status` | Endpoint MỚI (image-api không có — editor dùng realtime); `params` projection strip `admin_ref`/`sid` | Spec 07; 429 RATE_LIMITED deferred (note trong spec 07) |
| `GET /api/editor/actors` | Endpoint MỚI editor-native (image-api không có) — read-only `actors` rows theo `snapshot_id`, no pipeline-completeness filter | Spec 10; casting resolve phía App (chốt 260812) — sub-app materialize client-side lúc create-remix |

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
