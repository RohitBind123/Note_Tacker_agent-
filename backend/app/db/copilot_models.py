"""SQLAlchemy models for the Phase 2 interactive meeting copilot.

A separate module from the core meeting models so the copilot domain — live
chat capture, transcript chunks + embeddings, rolling meeting memory, and
answered @mentions — stays cohesive. Imported at the bottom of
``app.db.models`` so these tables register on the shared ``Base.metadata``
(which Alembic's ``env.py`` and the app runtime both rely on).

Idempotency is designed in, not bolted on:
- ``MeetingChatMessage`` dedups on (meeting_id, dedup_key) so a message seen
  over BOTH the WebSocket and the polling fallback is stored once.
- ``CopilotInteraction`` is unique per triggering chat message, so a duplicate
  mention delivery never produces two replies in the meeting.
- ``TranscriptChunk`` is unique per (meeting_id, chunk_index) for idempotent
  re-embedding / upsert.
- ``MeetingMemory`` is one row per meeting, refreshed in place.
"""
from __future__ import annotations

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# Physical dimension of the transcript_chunks.embedding column. MUST equal
# settings.embed_dimensions (default 768). The column dimension is fixed at DDL
# time and cannot follow a runtime setting, so this is a module constant;
# changing it requires an ALTER + a re-embed backfill. The embeddings service
# asserts each vector matches this length before insert (fail loud, not silent
# truncation).
EMBED_DIM = 768


class CopilotInteractionStatus(str, enum.Enum):
    """Lifecycle of a single answered @mention."""

    PENDING = "pending"      # mention claimed, answer generation in flight
    ANSWERED = "answered"    # reply successfully posted to the meeting chat
    FAILED = "failed"        # generation or send failed (see ``error``)
    SKIPPED = "skipped"      # nothing to answer (empty question after the trigger)


class MeetingChatMessage(Base, TimestampMixin):
    """A single chat message captured from the live Meet chat.

    Persisted because it is part of the copilot's context AND because Vexa chat
    messages carry no stable id — ``dedup_key`` = sha256(sender|timestamp|text)
    is our idempotency anchor. The unique (meeting_id, dedup_key) constraint
    means the same message arriving over the WebSocket and again via the polling
    fallback is stored exactly once.
    """

    __tablename__ = "meeting_chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender: Mapped[str | None] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Vexa's message timestamp, stored verbatim (number or ISO string) to avoid
    # lossy conversion; chronological reads also use created_at.
    vexa_timestamp: Mapped[str | None] = mapped_column(String(64))
    is_from_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True when the text contains a configured copilot trigger (@centralagent).
    is_mention: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("meeting_id", "dedup_key", name="uq_chat_msg_meeting_dedup"),
        Index("ix_chat_msg_meeting_created", "meeting_id", "created_at"),
    )


class CopilotInteraction(Base, TimestampMixin):
    """One answered @mention — the copilot's idempotency anchor.

    Exactly one interaction per triggering chat message (unique
    ``chat_message_id``). The mention router INSERTs this row (ON CONFLICT DO
    NOTHING) and only proceeds to generate + send an answer if it won the
    insert, so a duplicate mention delivery never yields two replies.
    """

    __tablename__ = "copilot_interactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chat_message_id: Mapped[int] = mapped_column(
        ForeignKey("meeting_chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    asker: Mapped[str | None] = mapped_column(String(256))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CopilotInteractionStatus] = mapped_column(
        SAEnum(CopilotInteractionStatus, name="copilot_interaction_status",
               native_enum=False, length=16),
        default=CopilotInteractionStatus.PENDING,
        nullable=False,
        index=True,
    )
    model_used: Mapped[str | None] = mapped_column(String(64))
    # Ids of the transcript chunks retrieved as grounding context (audit/debug).
    context_chunk_ids: Mapped[list | None] = mapped_column(JSONB)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class TranscriptChunk(Base, TimestampMixin):
    """A chunk of the meeting transcript plus its embedding, for RAG retrieval.

    Chunks are produced incrementally during the meeting. Unique
    (meeting_id, chunk_index) makes re-embedding idempotent. The HNSW cosine
    index supports top-K similarity retrieval to ground copilot answers.
    """

    __tablename__ = "transcript_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(256))
    # Raw transcript-segment bounds (Vexa's absolute_start_time etc.), verbatim.
    start_time: Mapped[str | None] = mapped_column(String(64))
    end_time: Mapped[str | None] = mapped_column(String(64))
    char_count: Mapped[int | None] = mapped_column(Integer)
    # L2-normalised embedding (Gemini does not pre-normalise at <3072 dims, so we
    # normalise before storing → cosine distance is well-conditioned). NULL until
    # the embedding call succeeds (missing != zero-vector).
    embedding: Mapped[list | None] = mapped_column(Vector(EMBED_DIM))
    embed_model: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("meeting_id", "chunk_index", name="uq_chunk_meeting_index"),
        Index(
            "ix_transcript_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": "16", "ef_construction": "64"},
        ),
    )


class MeetingMemory(Base, TimestampMixin):
    """Rolling structured memory built from the live transcript.

    One row per meeting (unique ``meeting_id``), refreshed in place as the
    meeting progresses. Grounds copilot answers with the meeting's evolving
    decisions / action items / risks / open questions without re-reading the
    full transcript on every question.
    """

    __tablename__ = "meeting_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    summary: Mapped[str | None] = mapped_column(Text)
    decisions: Mapped[list | None] = mapped_column(JSONB)
    action_items: Mapped[list | None] = mapped_column(JSONB)
    risks: Mapped[list | None] = mapped_column(JSONB)
    open_questions: Mapped[list | None] = mapped_column(JSONB)
    # Transcript characters covered at last rebuild — lets the refresher skip
    # work when nothing material was added since the previous pass.
    transcript_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(64))
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
