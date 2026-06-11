"""Application configuration.

Single source of truth for all settings. EVERYTHING comes from the
environment (loaded from the project-root ``.env``) — nothing is hardcoded.
Import ``settings`` anywhere; never read ``os.environ`` directly elsewhere.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root is two levels up from this file: backend/app/config.py -> centralagent/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def _to_asyncpg_url(raw: str) -> str:
    """Convert a libpq/Neon URL to a SQLAlchemy+asyncpg URL.

    asyncpg does not understand libpq query params like ``sslmode`` or
    ``channel_binding`` — they must be stripped (SSL is configured via
    ``connect_args`` on the engine instead). The scheme is normalised to
    ``postgresql+asyncpg``.
    """
    if not raw:
        return raw
    parts = urlsplit(raw)
    scheme = "postgresql+asyncpg"
    # Drop the query string entirely (sslmode / channel_binding live there).
    return urlunsplit((scheme, parts.netloc, parts.path, "", ""))


class Settings(BaseSettings):
    """All runtime configuration, validated at import time."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: str = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Cadence (seconds) ---
    calendar_poll_interval_seconds: int = 60
    scheduler_interval_seconds: int = 30
    dispatch_lead_seconds: int = 60
    bot_status_poll_interval_seconds: int = 10
    # Grace period after a meeting's scheduled end_time before the scheduler
    # force-stops a still-active bot (so it never lingers in an empty Meet).
    meeting_end_grace_seconds: int = 120

    # Who receives the insight email: "organizer" (default) or "all_attendees"
    # (organizer + every invited guest, excluding the bot itself).
    email_recipients: str = Field(default="organizer", alias="EMAIL_RECIPIENTS")

    # --- Database (Neon) ---
    # Pooled URL for the app runtime; direct (non-pooler) URL for migrations.
    database_url: str = Field(..., alias="DATABASE_URL")
    database_url_direct: str = Field(default="", alias="DATABASE_URL_DIRECT")

    # --- Vexa (cloud meeting bot API) ---
    vexa_api_base: str = Field(default="https://api.cloud.vexa.ai", alias="VEXA_API_BASE")
    vexa_api_key: str = Field(default="", alias="VEXA_API_KEY")
    vexa_transcription_token: str = Field(default="", alias="VEXA_TRANSCRIPTION_TOKEN")
    # How long the bot lingers alone in a Meet before leaving (Vexa default is 900s
    # = 15 min, which delays insight delivery). Lower = insights arrive sooner after
    # everyone leaves. Sent as automatic_leave.max_time_left_alone (ms) on dispatch.
    vexa_leave_when_alone_seconds: int = Field(default=45, alias="VEXA_LEAVE_WHEN_ALONE_SECONDS")
    # How long to wait for anyone to join before giving up (Vexa default 120s).
    vexa_no_one_joined_timeout_seconds: int = Field(
        default=300, alias="VEXA_NO_ONE_JOINED_TIMEOUT_SECONDS"
    )

    # --- Gemini ---
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemini_api_base: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        alias="GEMINI_API_BASE",
    )

    # --- Google (Calendar read + Gmail send) ---
    gcp_project_id: str = Field(default="", alias="GCP_PROJECT_ID")
    bot_google_email: str = Field(default="", alias="BOT_GOOGLE_EMAIL")
    google_oauth_client_id: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_SECRET")
    google_oauth_refresh_token: str = Field(default="", alias="GOOGLE_OAUTH_REFRESH_TOKEN")

    # --- Webhooks / public URL ---
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    # Calendar push (events.watch). Requires a VERIFIED-domain HTTPS public_base_url;
    # stays off in dev (ngrok can't be domain-verified) -> poller is the fallback.
    calendar_push_enabled: bool = Field(default=False, alias="CALENDAR_PUSH_ENABLED")
    calendar_webhook_token: str = Field(default="", alias="CALENDAR_WEBHOOK_TOKEN")

    # ----- Derived URLs (asyncpg) -----
    @property
    def async_database_url(self) -> str:
        """Pooled connection for the running app (asyncpg)."""
        return _to_asyncpg_url(self.database_url)

    @property
    def async_database_url_direct(self) -> str:
        """Direct (non-pooler) connection for Alembic migrations (asyncpg)."""
        target = self.database_url_direct or self.database_url
        return _to_asyncpg_url(target)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    def missing_required(self) -> list[str]:
        """Names of critical settings that are unset (used for fail-fast)."""
        required = {
            "DATABASE_URL": self.database_url,
            "VEXA_API_KEY": self.vexa_api_key,
            "GEMINI_API_KEY": self.gemini_api_key,
            "GOOGLE_OAUTH_CLIENT_ID": self.google_oauth_client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": self.google_oauth_client_secret,
            "GOOGLE_OAUTH_REFRESH_TOKEN": self.google_oauth_refresh_token,
            "BOT_GOOGLE_EMAIL": self.bot_google_email,
        }
        return [name for name, value in required.items() if not value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return Settings()


settings = get_settings()
