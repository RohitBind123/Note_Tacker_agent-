"""BotProvider abstraction.

Lets the rest of the system depend on a stable interface while we swap the
concrete engine: ``CloudVexaProvider`` (dev, via api.cloud.vexa.ai) now, and a
``SelfHostVexaProvider`` (signed-in bot for zero-click auto-admit) later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class BotDispatchResult:
    """Outcome of asking the provider to send a bot."""

    vexa_bot_id: str
    status: str           # provider's raw status, e.g. "requested"
    raw: dict = field(default_factory=dict)


@dataclass
class BotStatusResult:
    """Current state of a running bot/meeting."""

    status: str           # provider's raw status, e.g. joining/awaiting_admission/active
    participants_count: int | None = None
    has_recording: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class TranscriptResult:
    """A fetched transcript."""

    segments: list = field(default_factory=list)
    full_text: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ChatMessage:
    """A single Meet chat message captured by the bot.

    ``timestamp`` is kept as the provider's raw value stringified (lossless)
    rather than parsed into a datetime, because it doubles as part of the
    idempotency key for chat capture.
    """

    sender: str
    text: str
    timestamp: str | None = None
    is_from_bot: bool = False
    raw: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    """Raised when the provider returns an unexpected/error response."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class BotProvider(ABC):
    """Stable interface for sending bots and retrieving transcripts."""

    @abstractmethod
    async def join(
        self, native_meeting_id: str, *, platform: str = "google_meet", bot_name: str | None = None
    ) -> BotDispatchResult:
        ...

    @abstractmethod
    async def get_status(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> BotStatusResult | None:
        """Return current status, or ``None`` if the bot is no longer active."""

    @abstractmethod
    async def get_transcript(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> TranscriptResult:
        ...

    @abstractmethod
    async def stop(self, native_meeting_id: str, *, platform: str = "google_meet") -> bool:
        ...

    # --- Phase 2: interactive copilot chat I/O ---

    @abstractmethod
    async def get_chat(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> list[ChatMessage]:
        """Return chat messages captured from the meeting (polling fallback)."""

    @abstractmethod
    async def send_chat(
        self, native_meeting_id: str, text: str, *, platform: str = "google_meet"
    ) -> bool:
        """Post a message into the meeting chat as the bot. True on success."""

    @abstractmethod
    async def set_webhook(self, webhook_url: str, webhook_secret: str) -> bool:
        """Register the account-level webhook (meeting.completed et al.)."""
