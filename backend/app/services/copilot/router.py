"""Mention router — turns a captured @mention into exactly one chat reply.

The idempotency anchor is ``CopilotInteraction.chat_message_id`` (unique). The
router INSERTs the interaction with ``ON CONFLICT DO NOTHING RETURNING id`` and
only proceeds to generate + send an answer if it WON that insert. So a mention
delivered over both the WebSocket and the polling fallback — or replayed by a
retry — yields one answer, never two.

Lifecycle of the claimed interaction:
  PENDING  -> claimed, generation in flight
  SKIPPED  -> the message was only the handle, nothing to ask
  ANSWERED -> reply posted to the meeting chat
  FAILED   -> generation or send failed (``error`` records why)

Context for the answer is assembled here (retrieval + memory + recent chat +
metadata) and handed to the engine; the per-answer grounding chunk ids are
stored on the interaction for audit.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.copilot_models import (
    CopilotInteraction,
    CopilotInteractionStatus,
    MeetingChatMessage,
    MeetingMemory,
)
from app.db.models import Meeting
from app.logging_config import get_logger
from app.services.copilot.engine import CopilotContext, CopilotEngine
from app.services.copilot.retrieval import retrieve_context
from app.services.gemini.embeddings import GeminiEmbedder
from app.services.vexa.provider import BotProvider

log = get_logger(__name__)

_RECENT_CHAT_LIMIT = 6


async def _claim_interaction(
    db: AsyncSession, meeting_id: int, chat_message_id: int, asker: str | None, question: str
) -> int | None:
    """Insert the interaction, winning the race or returning None if already taken."""
    stmt = (
        pg_insert(CopilotInteraction)
        .values(
            meeting_id=meeting_id,
            chat_message_id=chat_message_id,
            asker=asker,
            question=question,
            status=CopilotInteractionStatus.PENDING,
        )
        .on_conflict_do_nothing(index_elements=["chat_message_id"])
        .returning(CopilotInteraction.id)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.scalar_one_or_none()


async def _finalize(
    db: AsyncSession,
    interaction_id: int,
    *,
    status: CopilotInteractionStatus,
    answer: str | None = None,
    model_used: str | None = None,
    context_chunk_ids: list[int] | None = None,
    error: str | None = None,
) -> None:
    values: dict = {"status": status, "error": error}
    if answer is not None:
        values["answer"] = answer
    if model_used is not None:
        values["model_used"] = model_used
    if context_chunk_ids is not None:
        values["context_chunk_ids"] = context_chunk_ids
    if status == CopilotInteractionStatus.ANSWERED:
        values["answered_at"] = datetime.now(UTC)
    await db.execute(
        update(CopilotInteraction).where(CopilotInteraction.id == interaction_id).values(**values)
    )
    await db.commit()


async def _recent_chat(db: AsyncSession, meeting_id: int, exclude_id: int) -> list[str]:
    """Last few human chat lines (oldest first), excluding the triggering message."""
    result = await db.execute(
        select(MeetingChatMessage.sender, MeetingChatMessage.text)
        .where(
            MeetingChatMessage.meeting_id == meeting_id,
            MeetingChatMessage.id != exclude_id,
            MeetingChatMessage.is_from_bot.is_(False),
        )
        .order_by(MeetingChatMessage.created_at.desc())
        .limit(_RECENT_CHAT_LIMIT)
    )
    rows = result.all()
    return [f"{(s or '').strip()}: {t}" if s else t for s, t in reversed(rows)]


async def _assemble_context(
    db: AsyncSession,
    meeting: Meeting,
    question: str,
    chat_message_id: int,
    *,
    embedder: GeminiEmbedder | None,
    top_k: int,
) -> tuple[CopilotContext, list[int]]:
    retrieved = await retrieve_context(
        db, meeting.id, question, top_k=top_k, embedder=embedder
    )
    memory = (
        await db.execute(select(MeetingMemory).where(MeetingMemory.meeting_id == meeting.id))
    ).scalar_one_or_none()
    recent = await _recent_chat(db, meeting.id, chat_message_id)

    snippets = [
        f"{(c.speaker + ': ') if c.speaker else ''}{c.text}" for c in retrieved
    ]
    ctx = CopilotContext(
        meeting_title=meeting.title,
        memory_summary=memory.summary if memory else None,
        decisions=memory.decisions if memory else None,
        action_items=memory.action_items if memory else None,
        open_questions=memory.open_questions if memory else None,
        transcript_snippets=snippets or None,
        recent_chat=recent or None,
    )
    return ctx, [c.id for c in retrieved]


async def handle_mention(
    db: AsyncSession,
    meeting: Meeting,
    chat_message_id: int,
    asker: str | None,
    question: str,
    *,
    provider: BotProvider,
    engine: CopilotEngine | None = None,
    embedder: GeminiEmbedder | None = None,
    top_k: int | None = None,
) -> int | None:
    """Answer a single @mention exactly once. Returns the interaction id, or None
    if the mention was already handled (lost the idempotent claim)."""
    interaction_id = await _claim_interaction(
        db, meeting.id, chat_message_id, asker, question
    )
    if interaction_id is None:
        log.info("copilot_mention_already_handled", chat_message_id=chat_message_id)
        return None

    if not question.strip():
        await _finalize(db, interaction_id, status=CopilotInteractionStatus.SKIPPED)
        log.info("copilot_mention_empty_skipped", interaction_id=interaction_id)
        return interaction_id

    engine = engine or CopilotEngine()
    top_k = top_k or settings.copilot_context_top_k

    try:
        ctx, chunk_ids = await _assemble_context(
            db, meeting, question, chat_message_id, embedder=embedder, top_k=top_k
        )
        answer = await engine.answer(question, ctx)
    except Exception as exc:  # noqa: BLE001 - any failure -> FAILED, never a crash loop
        log.error("copilot_answer_error", interaction_id=interaction_id, error=str(exc))
        await _finalize(
            db, interaction_id, status=CopilotInteractionStatus.FAILED,
            model_used=engine.model, error=str(exc),
        )
        return interaction_id

    sent = await provider.send_chat(
        meeting.native_meeting_id, answer, platform=meeting.platform
    )
    await _finalize(
        db,
        interaction_id,
        status=CopilotInteractionStatus.ANSWERED if sent else CopilotInteractionStatus.FAILED,
        answer=answer,
        model_used=engine.model,
        context_chunk_ids=chunk_ids,
        error=None if sent else "send_chat returned False",
    )
    log.info(
        "copilot_mention_handled",
        interaction_id=interaction_id,
        sent=sent,
        answer_chars=len(answer),
        context_chunks=len(chunk_ids),
    )
    return interaction_id
