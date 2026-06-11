"""Unit tests for the Phase 2 copilot ORM models (Batch 1).

These assert the schema-level invariants that the migration also enforces:
table registration, the idempotency unique keys, and the embedding dimension.
"""
from sqlalchemy import UniqueConstraint

from app.config import settings
from app.db import models  # noqa: F401 - registers core + copilot tables
from app.db.base import Base
from app.db.copilot_models import EMBED_DIM

COPILOT_TABLES = {
    "meeting_chat_messages",
    "copilot_interactions",
    "transcript_chunks",
    "meeting_memory",
}


def test_copilot_tables_registered():
    assert COPILOT_TABLES <= set(Base.metadata.tables)


def test_embedding_dim_matches_config():
    # The physical vector column dimension must equal the requested Gemini dim,
    # otherwise inserts fail with a pgvector dimension mismatch.
    assert EMBED_DIM == settings.embed_dimensions == 768


def test_transcript_chunk_embedding_is_768_vector():
    emb = Base.metadata.tables["transcript_chunks"].columns["embedding"]
    assert getattr(emb.type, "dim", None) == 768


def _unique_col_sets(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    sets: set[tuple[str, ...]] = set()
    for c in table.constraints:
        if isinstance(c, UniqueConstraint):
            sets.add(tuple(sorted(col.name for col in c.columns)))
    return sets


def test_chat_message_dedup_unique_key():
    # (meeting_id, dedup_key) is the idempotency anchor for chat capture.
    assert ("dedup_key", "meeting_id") in _unique_col_sets("meeting_chat_messages")


def test_chunk_meeting_index_unique_key():
    # (meeting_id, chunk_index) makes re-embedding idempotent.
    assert ("chunk_index", "meeting_id") in _unique_col_sets("transcript_chunks")


def test_copilot_interaction_unique_per_chat_message():
    # One answer per triggering mention => never reply twice.
    col = Base.metadata.tables["copilot_interactions"].columns["chat_message_id"]
    assert col.unique is True


def test_meeting_memory_one_row_per_meeting():
    col = Base.metadata.tables["meeting_memory"].columns["meeting_id"]
    assert col.unique is True


def test_chat_message_has_mention_and_bot_flags():
    cols = set(Base.metadata.tables["meeting_chat_messages"].columns.keys())
    assert {"is_mention", "is_from_bot", "sender", "text", "vexa_timestamp"} <= cols
