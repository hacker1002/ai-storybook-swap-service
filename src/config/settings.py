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

    # --- DB pool (Phase 02) -------------------------------------------------
    app_db_pool_min: int = 2
    app_db_pool_max: int = 10
    app_db_statement_timeout_ms: int = 15000
    # A pre-existing auth.users row used as `background_jobs.user_id` (NOT NULL FK).
    # The service has no user directory; real attribution lives in params. Only
    # REQUIRED once jobs are inserted (P3b) — optional/empty at P3a.
    remix_swap_service_user_id: str = ""

    # --- Auth (Phase 03) ----------------------------------------------------
    editor_token_leeway_seconds: int = 30

    # --- Request / CORS -----------------------------------------------------
    # 20MB body cap for POST/PATCH (spec 04/05 payload-bomb guard).
    request_body_max_bytes: int = 20 * 1024 * 1024
    cors_allowed_origins: str = "http://localhost:5173"
    port: int = 8100

    # --- AI (optional at P3a, REQUIRED at P3b) ------------------------------
    google_cloud_project: str = ""
    replicate_api_token: str = ""
    langchain_api_key: str = ""

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
