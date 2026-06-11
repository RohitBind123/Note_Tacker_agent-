"""phase 2 copilot: chat messages, transcript chunks (pgvector), memory, interactions

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-06-11

Adds the four tables that back the interactive meeting copilot:

  - meeting_chat_messages : every Meet chat message captured (idempotency anchor
                            via the (meeting_id, dedup_key) unique constraint)
  - copilot_interactions  : one answered @mention per triggering chat message
                            (unique chat_message_id => never reply twice)
  - transcript_chunks     : transcript chunks + a vector(768) embedding for RAG
                            retrieval, with an HNSW cosine index
  - meeting_memory        : one rolling structured-memory row per meeting

Pre-migration data quality audit: all four tables are brand-new, so zero
existing rows can violate any of the new constraints (unique keys, NOT NULLs,
the status CHECK, the vector dimension). Safe to create directly.

Requires the pgvector extension. ``CREATE EXTENSION IF NOT EXISTS vector`` is
permitted on Neon. The HNSW index is built on an empty table, so it is instant.

Every statement uses IF NOT EXISTS so a partially-applied migration is safe to
re-run. The downgrade drops the tables in FK-dependency order but deliberately
does NOT drop the ``vector`` extension (other objects may depend on it).

Enum note: ``copilot_interactions.status`` mirrors a SQLAlchemy
``Enum(..., native_enum=False)`` which stores the member NAME, so the CHECK
lists the uppercase names and the default is 'PENDING' (matching the existing
meeting_status convention).
"""
from __future__ import annotations

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector extension (needed for the vector column + HNSW index).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- meeting_chat_messages -------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_chat_messages (
            id BIGSERIAL PRIMARY KEY,
            meeting_id BIGINT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            sender VARCHAR(256),
            text TEXT NOT NULL,
            vexa_timestamp VARCHAR(64),
            is_from_bot BOOLEAN NOT NULL DEFAULT FALSE,
            is_mention BOOLEAN NOT NULL DEFAULT FALSE,
            dedup_key VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_chat_msg_meeting_dedup UNIQUE (meeting_id, dedup_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_meeting_chat_messages_meeting_id "
        "ON meeting_chat_messages (meeting_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_msg_meeting_created "
        "ON meeting_chat_messages (meeting_id, created_at)"
    )

    # --- copilot_interactions --------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS copilot_interactions (
            id BIGSERIAL PRIMARY KEY,
            meeting_id BIGINT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            chat_message_id BIGINT NOT NULL UNIQUE
                REFERENCES meeting_chat_messages(id) ON DELETE CASCADE,
            asker VARCHAR(256),
            question TEXT NOT NULL,
            answer TEXT,
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            model_used VARCHAR(64),
            context_chunk_ids JSONB,
            answered_at TIMESTAMPTZ,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_copilot_interaction_status
                CHECK (status IN ('PENDING', 'ANSWERED', 'FAILED', 'SKIPPED'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_copilot_interactions_meeting_id "
        "ON copilot_interactions (meeting_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_copilot_interactions_status "
        "ON copilot_interactions (status)"
    )

    # --- transcript_chunks (pgvector) -----------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_chunks (
            id BIGSERIAL PRIMARY KEY,
            meeting_id BIGINT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            speaker VARCHAR(256),
            start_time VARCHAR(64),
            end_time VARCHAR(64),
            char_count INTEGER,
            embedding vector(768),
            embed_model VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_chunk_meeting_index UNIQUE (meeting_id, chunk_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcript_chunks_meeting_id "
        "ON transcript_chunks (meeting_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcript_chunks_embedding_hnsw "
        "ON transcript_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # --- meeting_memory --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_memory (
            id BIGSERIAL PRIMARY KEY,
            meeting_id BIGINT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            summary TEXT,
            decisions JSONB,
            action_items JSONB,
            risks JSONB,
            open_questions JSONB,
            transcript_chars INTEGER NOT NULL DEFAULT 0,
            model_used VARCHAR(64),
            refreshed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_meeting_memory_meeting_id "
        "ON meeting_memory (meeting_id)"
    )


def downgrade() -> None:
    # FK-dependency order: interactions -> chat_messages; chunks & memory stand
    # alone. The vector extension is intentionally left installed.
    op.execute("DROP TABLE IF EXISTS copilot_interactions")
    op.execute("DROP TABLE IF EXISTS transcript_chunks")
    op.execute("DROP TABLE IF EXISTS meeting_memory")
    op.execute("DROP TABLE IF EXISTS meeting_chat_messages")
