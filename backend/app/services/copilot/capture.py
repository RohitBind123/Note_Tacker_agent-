"""Live chat capture + mention routing.

Pulls chat messages off the provider (WebSocket push or REST poll), persists
each one exactly once, and routes brand-new @mentions to the answer engine.

Idempotency is layered so the same message arriving twice (WS + poll, or a
provider retry) never produces a duplicate row or a duplicate reply:

  1. ``MeetingChatMessage`` is unique on (meeting_id, dedup_key) where
     dedup_key = sha256(sender|timestamp|text). The insert is
     ON CONFLICT DO NOTHING RETURNING id, so only the first delivery yields an
     id — and only that delivery is routed.
  2. ``CopilotInteraction`` is unique per chat_message_id, so even if routing
     somehow fired twice, the mention router (see ``handle_mention``) still
     answers once.

Capture never raises out of the loop: a single bad message is logged and
skipped so it can't stall live capture for the whole meeting.
"""
from __future__ import annotations

import hashlib

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.copilot_models import MeetingChatMessage
from app.db.models import Meeting
from app.logging_config import get_logger
from app.services.copilot.engine import CopilotEngine
from app.services.copilot.router import handle_mention
from app.services.copilot.triggers import parse_mention
from app.services.gemini.embeddings import GeminiEmbedder
from app.services.vexa.provider import BotProvider, ChatMessage

log = get_logger(__name__)


def compute_dedup_key(sender: str | None, timestamp: str | None, text: str) -> str:
    """Stable idempotency key for a chat message.

    Vexa chat messages carry no stable id, so we derive one from the content the
    provider does give us. sha256 hex is 64 chars — exactly the dedup_key column
    width. Same (sender, timestamp, text) -> same key -> deduped on insert.
    """
    material = f"{sender or ''}|{timestamp or ''}|{text or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _persist_message(
    db: AsyncSession, meeting_id: int, msg: ChatMessage, *, is_mention: bool
) -> int | None:
    """Insert the message idempotently. Returns its id if NEW, else None."""
    dedup_key = compute_dedup_key(msg.sender, msg.timestamp, msg.text)
    stmt = (
        pg_insert(MeetingChatMessage)
        .values(
            meeting_id=meeting_id,
            sender=(msg.sender or None),
            text=msg.text,
            vexa_timestamp=msg.timestamp,
            is_from_bot=msg.is_from_bot,
            is_mention=is_mention,
            dedup_key=dedup_key,
        )
        .on_conflict_do_nothing(index_elements=["meeting_id", "dedup_key"])
        .returning(MeetingChatMessage.id)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.scalar_one_or_none()


async def capture_chat(
    db: AsyncSession,
    meeting: Meeting,
    *,
    provider: BotProvider,
    engine: CopilotEngine | None = None,
    embedder: GeminiEmbedder | None = None,
    triggers: list[str] | None = None,
    top_k: int | None = None,
) -> int:
    """Capture all chat messages for a meeting, routing new @mentions.

    Returns the number of newly-persisted messages. Existing messages (already
    captured on a previous tick) are skipped cheaply by the dedup insert.
    """
    triggers = triggers if triggers is not None else settings.copilot_triggers
    messages = await provider.get_chat(meeting.native_meeting_id, platform=meeting.platform)
    if not messages:
        return 0

    new_count = 0
    for msg in messages:
        try:
            parsed = parse_mention(msg.text, triggers)
            # Only a human's @mention is actionable: never route the bot's own
            # posts (it echoes its answers into the chat).
            actionable = parsed.is_mention and not msg.is_from_bot
            message_id = await _persist_message(
                db, meeting.id, msg, is_mention=actionable
            )
            if message_id is None:
                continue  # already captured on an earlier tick / other channel
            new_count += 1
            if actionable:
                await handle_mention(
                    db,
                    meeting,
                    message_id,
                    msg.sender,
                    parsed.question,
                    provider=provider,
                    engine=engine,
                    embedder=embedder,
                    top_k=top_k,
                )
        except Exception:  # noqa: BLE001 - one bad message must not stall capture
            log.exception(
                "copilot_capture_message_error",
                meeting_id=meeting.id,
                sender=msg.sender,
            )

    if new_count:
        log.info("copilot_chat_captured", meeting_id=meeting.id, new_messages=new_count)
    return new_count
