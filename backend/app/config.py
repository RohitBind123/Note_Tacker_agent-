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
    # Fallback insight-email recipient when no human organizer/attendee can be
    # resolved (e.g. a meet.google.com instant invite arrives only From Google's
    # no-reply address). Empty -> no fallback (meeting marked EMAIL_FAILED).
    # This is an explicit, configured address, so it is honoured even if it equals
    # BOT_GOOGLE_EMAIL (the bot mailing its own inbox is a valid, readable target).
    report_fallback_email: str = Field(default="", alias="REPORT_FALLBACK_EMAIL")

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
    # Embedding model for the copilot retrieval layer (RAG over transcript chunks).
    gemini_embed_model: str = Field(
        default="gemini-embedding-001", alias="GEMINI_EMBED_MODEL"
    )
    # Output dimensionality for embeddings. 768 keeps us under pgvector's 2000-dim
    # HNSW index limit; NOT pre-normalised by Gemini at <3072, so we L2-normalise
    # ourselves before storing/comparing (cosine). Changing this requires a
    # migration (the vector column dimension is fixed) + a re-embed backfill.
    embed_dimensions: int = Field(default=768, ge=128, le=3072, alias="EMBED_DIMENSIONS")

    # --- Phase 2: Interactive Meeting Copilot ---
    # Master switch. OFF by default so the copilot ships dark and is enabled per
    # environment once a live meeting has validated the WS + chat round-trip.
    copilot_enabled: bool = Field(default=False, alias="COPILOT_ENABLED")
    # Comma-separated mention triggers. A chat message is routed to the copilot
    # only if its text contains one of these (case-insensitive). Parsed via
    # the ``copilot_triggers`` property below.
    copilot_triggers_raw: str = Field(default="@centralagent", alias="COPILOT_TRIGGERS")
    # The bot's visible display name in the Meet roster + chat. Must align with a
    # trigger so participants can discover how to summon it ("@CentralAgent ...").
    copilot_bot_name: str = Field(default="CentralAgent", alias="COPILOT_BOT_NAME")
    # Vexa real-time WebSocket (PRIMARY live channel for chat.received +
    # transcript.mutable). Polling is the documented fallback only.
    vexa_ws_url: str = Field(default="wss://api.cloud.vexa.ai/ws", alias="VEXA_WS_URL")
    # Fallback chat-poll cadence used only when the WS is unavailable/disconnected.
    copilot_chat_poll_interval_seconds: int = Field(
        default=8, ge=2, le=120, alias="COPILOT_CHAT_POLL_INTERVAL_SECONDS"
    )
    # How often the rolling meeting-memory (decisions/action items/risks/open
    # questions) is rebuilt from the growing transcript during a live meeting.
    copilot_memory_refresh_seconds: int = Field(
        default=60, ge=15, le=600, alias="COPILOT_MEMORY_REFRESH_SECONDS"
    )
    # Number of transcript chunks retrieved (top-K by cosine similarity) to ground
    # a copilot answer.
    copilot_context_top_k: int = Field(default=6, ge=1, le=50, alias="COPILOT_CONTEXT_TOP_K")
    # Shared secret for verifying inbound Vexa webhook HMAC signatures. Empty ->
    # the webhook endpoint rejects all calls (fail closed).
    vexa_webhook_secret: str = Field(default="", alias="VEXA_WEBHOOK_SECRET")

    @property
    def copilot_triggers(self) -> list[str]:
        """Normalised (lowercased, stripped, non-empty) mention triggers."""
        return [
            token.strip().lower()
            for token in self.copilot_triggers_raw.split(",")
            if token.strip()
        ]

    # --- Google (Calendar read + Gmail send) ---
    gcp_project_id: str = Field(default="", alias="GCP_PROJECT_ID")
    bot_google_email: str = Field(default="", alias="BOT_GOOGLE_EMAIL")
    google_oauth_client_id: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_SECRET")
    google_oauth_refresh_token: str = Field(default="", alias="GOOGLE_OAUTH_REFRESH_TOKEN")

    # --- Gmail invite scanner ---
    # Reads the bot's Gmail inbox for Meet invite emails sent via "Add people"
    # in meet.google.com (which creates no Calendar event, so the calendar poller
    # misses them). Default OFF: requires gmail.readonly to be added to the refresh
    # token's OAuth scope before enabling (see docs/CHALLENGES.md).
    gmail_scan_enabled: bool = Field(default=False, alias="GMAIL_SCAN_ENABLED")
    gmail_scan_interval_seconds: int = Field(default=90, alias="GMAIL_SCAN_INTERVAL_SECONDS")
    # ge=1 prevents maxResults=0 which the Gmail API rejects with HTTP 400.
    gmail_scan_max_results: int = Field(default=25, ge=1, le=500, alias="GMAIL_SCAN_MAX_RESULTS")
    # Tune the Gmail search query without a deploy.
    # Scoped to meetings-noreply@google.com — the sender Google Meet uses for
    # "Add people" / instant-meeting invites, which create NO Calendar event (so
    # the calendar poller is blind to them). Calendar invitations come from the
    # organizer instead, so they are intentionally excluded here to avoid the
    # scanner creating a duplicate row for a meeting the poller already handles.
    # ROLLOUT NOTE: on first enable, widen to newer_than:7d so invites received
    # before the feature was turned on are not missed. Restore to newer_than:1d
    # after the first successful scan cycle.
    gmail_scan_query: str = Field(
        default='from:meetings-noreply@google.com "meet.google.com" newer_than:1d',
        alias="GMAIL_SCAN_QUERY",
    )

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
