"""Application settings loaded from environment variables (Pydantic Settings).

Boundary vs `ai-storybook-image-api`: this service is a DELIBERATE BE-layer fork
(ADR-052). It shares NO code with image-api — only the API contract. There is NO
Supabase SDK config here; DB access is direct asyncpg (`APP_DB_URL`).
"""

from functools import cached_property

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
    # Shared HS256 secret with the Admin App backend that MINTS editor-session
    # tokens. Distinct from Supabase JWT secret AND player token secret. Accepts a
    # comma-separated LIST for rotation (old,new) — see `editor_token_secrets`.
    remix_editor_token_secret: str

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

    # --- Dev mint (spec 10 — DEV ONLY stand-in for the Admin App backend mint) ---
    # Default OFF: the /api/dev router is NOT registered unless this is true, so a
    # prod deploy without the flag has no mint surface at all. NEVER enable in prod.
    dev_mint_enabled: bool = False
    # Gate key for POST /api/dev/mint-editor-token (X-Dev-Mint-Key header). A
    # secret in its own right within a dev instance (leaking it = arbitrary admin
    # sessions there). MUST NOT reuse/derive from remix_editor_token_secret.
    # Required (fail-fast at boot) when dev_mint_enabled.
    dev_mint_key: str = ""

    # --- Request / CORS -----------------------------------------------------
    # 20MB body cap for POST/PATCH (spec 04/05 payload-bomb guard).
    request_body_max_bytes: int = 20 * 1024 * 1024
    cors_allowed_origins: str = "http://localhost:5173"
    port: int = 8100

    # --- Storage (Supabase Storage REST via httpx — NO SDK) -----------------
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
