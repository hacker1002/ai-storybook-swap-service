"""Application settings loaded from environment variables (Pydantic Settings).

Boundary vs `ai-storybook-image-api`: this service is a DELIBERATE BE-layer fork
(ADR-052). It shares NO code with image-api — only the API contract. There is NO
Supabase SDK config here; DB access is direct asyncpg (`APP_DB_URL`).
"""

from functools import cached_property

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the Remix Swap Service.

    Fail-fast on the two REQUIRED secrets (`app_db_url`, `remix_editor_token_secret`)
    — a missing value raises at construction so boot dies with a clear message
    instead of surfacing as opaque 401/500s at request time.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- REQUIRED -----------------------------------------------------------
    # DSN Postgres. P3a: temporarily the local Supabase DB
    # (postgresql://postgres:postgres@127.0.0.1:54322/postgres). Contains
    # credentials → never logged (log host/db name only).
    app_db_url: str
    # HS256 secret used to MINT + verify editor-session access tokens. After ADR-053
    # this service OWNS minting (exchange endpoint), so the secret is LOCAL-ONLY —
    # never shared with the Admin App, never logged, never returned to a client.
    # Accepts a comma-separated LIST for rotation (old,new) — see `editor_token_secrets`
    # (mint uses the LAST entry; verify accepts any).
    remix_editor_token_secret: str
    # Shared HS256 secret with the Admin App backend that signs the short-lived
    # HANDOFF ASSERTION (aud=remix-editor-handoff) exchanged at /api/editor/auth/exchange.
    # THIS is the only auth secret shared with App. Comma-list for rotation.
    remix_editor_handoff_secret: str

    # --- DB pool (Phase 02; ceiling raised to 20 in P3b — jobs run concurrent AI
    #     handlers, acquire-per-query, so the pool must not starve under load) ----
    app_db_pool_min: int = 2
    app_db_pool_max: int = 20
    app_db_statement_timeout_ms: int = 15000
    # Heartbeat beat period for long single-`await` job handlers (helpers/heartbeat).
    # MUST keep ≥3× margin under the reaper stale threshold (REAPER_STALE_SEC=1800).
    job_heartbeat_sec: int = 30
    # A pre-existing auth.users row used as `background_jobs.user_id` (NOT NULL FK).
    # The service has no user directory; real attribution lives in params. Only
    # REQUIRED once jobs are inserted (P3b) — optional/empty at P3a.
    remix_swap_service_user_id: str = ""

    # --- Auth (Phase 03) ----------------------------------------------------
    editor_token_leeway_seconds: int = 30
    # Access-token lifetime minted at exchange. Flat 12h (ADR-053 — no refresh).
    editor_access_token_ttl_seconds: int = 43200
    # S2S key guarding POST /internal/auth/revoke. Empty => FAIL-CLOSED (revoke
    # disabled, boot warning) so the service still runs before Admin App P2 exists.
    internal_api_key: str = ""
    # Per-IP sliding-window cap on the public (no-Bearer) exchange endpoint.
    auth_exchange_rate_limit_per_min: int = 20

    # --- Request / CORS -----------------------------------------------------
    # 20MB body cap for POST/PATCH (spec 04/05 payload-bomb guard).
    request_body_max_bytes: int = 20 * 1024 * 1024
    cors_allowed_origins: str = "http://localhost:5173"
    port: int = 8100

    # --- Storage (LEGACY Supabase Storage REST via httpx — NO SDK; rollback) --
    # Used ONLY when the STORAGE_SERVICE_* cluster below is empty (ADR-054 switch).
    # Base Supabase URL (same host that serves /storage/v1/...). Local P3b:
    # http://127.0.0.1:54321. Optional at P3a boot; the storage adapter is only
    # exercised once a ported endpoint uploads (P3b).
    app_storage_url: str = ""
    # Service-role key for Storage writes. SECRET — never logged / never to client.
    app_storage_service_key: str = ""
    # Reuse the EXISTING editor bucket (verified: image-api uploader
    # STORAGE_BUCKET = "storybook-assets") — no new bucket, URL pattern + SSRF
    # allowlist unchanged.
    app_storage_bucket: str = "storybook-assets"
    # Comma-separated SSRF allowlist of host or host:port entries that bypass the
    # private-IP guard (local: "127.0.0.1:54321" so the service can re-fetch its
    # own Storage uploads). Empty in prod (public *.supabase.co resolves publicly).
    ssrf_allowed_hosts: str = ""

    # --- Storage Service (ADR-054 — env-presence switch, mirrors image-api) ------
    # PRESENCE-SWITCH: `storage_service_url` non-empty ⇒ every write/sign/delete goes
    # through the self-hosted storage service (loopback S2S, `X-API-Key`) instead of
    # Supabase Storage. All THREE empty ⇒ legacy Supabase path (the rollback state).
    # The single `AppStorageAdapter` seam is unchanged — only the wired impl flips.
    #   - `storage_service_url`      base for write/sign/delete S2S. LOOPBACK (prod
    #                                127.0.0.1:8200). ⚠️ A public domain 403s writes
    #                                (nginx proxies READ only) — never point this at it.
    #   - `storage_service_api_key`  the `swap-service` key from the service STORAGE_API_KEYS
    #   - `storage_public_base_url`  base to build persisted READ URLs
    #                                `{base}/files/{bucket}/{key}` (public domain / nginx)
    # Trailing slashes normalized off (field_validator) so `{base}/files/...` never
    # double-slashes. HALF the cluster set (service_url without key/public base) is a
    # FAIL-FAST at boot (model_validator) — a silent legacy fallback would leave prod
    # "thinking it cut over while still writing Supabase". Rollback = clear ALL three.
    storage_service_url: str = ""
    storage_service_api_key: str = ""
    storage_public_base_url: str = ""
    # Optional loopback-nginx base to READ blob bytes back (ADR-054, mirrors
    # image-api — prod-proven there): when set, `to_fetch_url()` rewrites a
    # persisted `{storage_public_base_url}/files/...` URL to this base at
    # fetch-time (combine-sheet re-fetch, audio chunks, ...) so reads skip the
    # public-domain egress hop. Empty = no rewrite. NOT part of the fail-fast
    # trio — purely additive; nothing rewritten is ever persisted.
    storage_internal_read_base_url: str = ""

    # --- AI (optional at P3a, REQUIRED at P3b) ------------------------------
    google_cloud_project: str = ""
    replicate_api_token: str = ""
    langchain_api_key: str = ""
    # ElevenLabs TTS/TTV (narration + voice-design pipeline, faithfully ported
    # from image-api `settings.elevenlabs_api_key`). Safe empty default so
    # import/construction never fails when the env var is unset; the audio
    # handlers surface an upstream auth error at call-time instead of boot-fail.
    elevenlabs_api_key: str = ""
    # Vertex AI region for Gemini (ADR-048). Not a secret.
    vertex_ai_location: str = "us-central1"
    # LangSmith tracing — dedicated project, separate from image-api's.
    langchain_project: str = "remix-swap-service"
    langchain_tracing_v2: str = ""
    langchain_endpoint: str = ""

    @field_validator(
        "storage_service_url",
        "storage_public_base_url",
        "storage_internal_read_base_url",
        mode="after",
    )
    @classmethod
    def _strip_storage_url_slash(cls, v: str) -> str:
        """Normalize storage base URLs: strip surrounding whitespace + trailing `/`
        so `{base}/files/{bucket}/{key}` never produces a `//files/`."""
        return (v or "").strip().rstrip("/")

    @model_validator(mode="after")
    def _validate_storage_service_cluster(self) -> "Settings":
        """FAIL-FAST (ADR-054): if the storage service is switched ON
        (`storage_service_url` set) but the cluster is only HALF configured, refuse
        to boot. A silent legacy fallback would let prod believe it cut over while
        still writing Supabase. Rollback = clear the WHOLE cluster (all three empty)."""
        if self.storage_service_url:
            missing = [
                name
                for name, val in (
                    ("STORAGE_SERVICE_API_KEY", self.storage_service_api_key),
                    ("STORAGE_PUBLIC_BASE_URL", self.storage_public_base_url),
                )
                if not (val or "").strip()
            ]
            if missing:
                raise ValueError(
                    "STORAGE_SERVICE_URL is set but "
                    f"{', '.join(missing)} is empty — the storage-service cluster is "
                    "half-configured. Set all of STORAGE_SERVICE_URL / "
                    "STORAGE_SERVICE_API_KEY / STORAGE_PUBLIC_BASE_URL, or clear ALL "
                    "three to run the legacy Supabase Storage path (ADR-054)."
                )
        return self

    @cached_property
    def ssrf_allowed_hosts_list(self) -> list[str]:
        """Parsed SSRF allowlist (host or host:port). Empties dropped."""
        return [h.strip().lower() for h in self.ssrf_allowed_hosts.split(",") if h.strip()]

    @cached_property
    def editor_token_secrets(self) -> list[str]:
        """Rotation-ready secret list. Designed as a list from day one so adding a
        second secret during a rotation window is NOT a contract change. Splits the
        comma-separated `remix_editor_token_secret`; empties dropped."""
        return [s.strip() for s in self.remix_editor_token_secret.split(",") if s.strip()]

    @cached_property
    def editor_handoff_secrets(self) -> list[str]:
        """Rotation-ready handoff-assertion secret list (mirrors `editor_token_secrets`).
        Verify tries each; the Admin App signs with one of them."""
        return [s.strip() for s in self.remix_editor_handoff_secret.split(",") if s.strip()]

    @cached_property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]  # required fields come from env/.env


def _export_langsmith_env() -> None:
    """Bridge LangSmith config from `.env` (loaded by pydantic into `settings`) to
    `os.environ`, which is where the `langsmith`/`langchain` SDK actually reads
    tracing config. pydantic-settings parses `.env` itself and does NOT populate
    `os.environ`, and `uv run` doesn't load `.env` either — so without this the
    tracer stays OFF (no traces) even though the keys are in `.env`. `setdefault`
    lets a real shell env win."""
    import os

    for name, value in (
        ("LANGCHAIN_TRACING_V2", settings.langchain_tracing_v2),
        ("LANGCHAIN_API_KEY", settings.langchain_api_key),
        ("LANGCHAIN_PROJECT", settings.langchain_project),
        ("LANGCHAIN_ENDPOINT", settings.langchain_endpoint),
    ):
        if value:
            os.environ.setdefault(name, value)


_export_langsmith_env()
