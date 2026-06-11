"""Copilot live ticks — driven by the background runner while a meeting is on.

Two cadences, mapped 1:1 onto the two config knobs so each has a single job:

  ``copilot_chat_tick`` (fast, COPILOT_CHAT_POLL_INTERVAL_SECONDS)
    Capture chat and answer @mentions, and keep the retrieval index fresh by
    embedding any new transcript chunks. This is the responsiveness path —
    participants expect a reply within a few seconds.

  ``copilot_memory_tick`` (slow, COPILOT_MEMORY_REFRESH_SECONDS)
    Rebuild the rolling meeting memory (summary / decisions / action items /
    risks / open questions). This is the paid Gemini build; it is additionally
    cost-guarded by a transcript-growth threshold, so a slow cadence plus the
    growth guard keeps spend bounded.

Both iterate the same set of live meetings (JOINING/ACTIVE with a dispatched
bot), each in its own session, and never let one meeting's error stop the
others — identical resilience discipline to the scheduler.
"""
from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.db.models import Meeting, MeetingStatus
from app.db.session import async_session_factory
from app.logging_config import get_logger
from app.services.copilot import capture
from app.services.copilot.engine import CopilotEngine
from app.services.copilot.memory import MeetingMemoryBuilder, refresh_memory
from app.services.copilot.retrieval import index_transcript
from app.services.gemini.embeddings import GeminiEmbedder
from app.services.vexa.factory import get_provider
from app.services.vexa.provider import BotProvider

log = get_logger(__name__)

# A meeting is "live" (worth polling for chat / indexing) while its bot is in
# flight. PROCESSING/finished meetings are out — their transcript is finalized
# by the orchestrator, not the copilot loops.
_LIVE_STATUSES = (MeetingStatus.JOINING, MeetingStatus.ACTIVE)


async def _live_meeting_ids() -> list[int]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Meeting.id).where(
                    Meeting.status.in_(_LIVE_STATUSES),
                    Meeting.vexa_bot_id.is_not(None),
                )
            )
        ).all()
    return [r[0] for r in rows]


async def copilot_chat_tick(*, provider: BotProvider | None = None) -> None:
    """Capture chat + answer mentions + index new transcript chunks (fast path)."""
    if not settings.copilot_enabled:
        return
    meeting_ids = await _live_meeting_ids()
    if not meeting_ids:
        return

    provider = provider or get_provider()
    # Construct the embedder/engine once per tick; a missing key fails fast here
    # rather than once per meeting. Both are stateless beyond config.
    embedder = GeminiEmbedder()
    engine = CopilotEngine()

    for meeting_id in meeting_ids:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                continue
            try:
                await capture.capture_chat(
                    db, meeting, provider=provider, engine=engine, embedder=embedder
                )
            except Exception:
                log.exception("copilot_chat_capture_error", meeting_id=meeting_id)

            try:
                transcript = await provider.get_transcript(
                    meeting.native_meeting_id, platform=meeting.platform
                )
                await index_transcript(
                    db, meeting.id, transcript.segments, embedder=embedder
                )
            except Exception:
                log.exception("copilot_index_error", meeting_id=meeting_id)


async def copilot_memory_tick(*, provider: BotProvider | None = None) -> None:
    """Rebuild the rolling meeting memory for each live meeting (slow path)."""
    if not settings.copilot_enabled:
        return
    meeting_ids = await _live_meeting_ids()
    if not meeting_ids:
        return

    provider = provider or get_provider()
    builder = MeetingMemoryBuilder()

    for meeting_id in meeting_ids:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                continue
            try:
                transcript = await provider.get_transcript(
                    meeting.native_meeting_id, platform=meeting.platform
                )
                await refresh_memory(db, meeting.id, transcript.full_text, builder=builder)
            except Exception:
                log.exception("copilot_memory_error", meeting_id=meeting_id)
