"""Embed-and-store transcript chunks + cosine top-K retrieval (pgvector).

Two operations:

  ``index_transcript`` — (re)chunk a meeting's transcript and embed/store only
  the chunks that don't exist yet. Idempotent on ``(meeting_id, chunk_index)``:
  re-running after the transcript grows embeds just the new tail, never the
  whole meeting again. Safe to call on every chat poll.

  ``retrieve_context`` — embed a copilot question and return the top-K most
  similar chunks by cosine distance, using the HNSW index on the vector column.

The chunker is deterministic from the transcript start (see ``chunker``), which
is what makes the incremental "embed only new chunk indices" approach correct:
chunk ``i``'s text is stable as the transcript grows, so a row already stored
for index ``i`` is still the right row.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.copilot_models import TranscriptChunk
from app.logging_config import get_logger
from app.services.copilot.chunker import Chunk, chunk_segments
from app.services.gemini.embeddings import GeminiEmbedder

log = get_logger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    """A retrieved chunk with its cosine distance (0 = identical, 2 = opposite)."""

    id: int
    chunk_index: int
    text: str
    speaker: str | None
    distance: float


async def _existing_chunk_count(db: AsyncSession, meeting_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(TranscriptChunk)
        .where(TranscriptChunk.meeting_id == meeting_id)
    )
    return int(result.scalar_one())


async def index_transcript(
    db: AsyncSession,
    meeting_id: int,
    raw_segments: list[dict],
    *,
    embedder: GeminiEmbedder | None = None,
) -> int:
    """Chunk + embed + store only the NEW chunks for a meeting. Returns # added.

    Idempotent: chunk indices already present are skipped (no re-embed), and the
    insert uses ``ON CONFLICT (meeting_id, chunk_index) DO NOTHING`` so a
    concurrent poller can't create duplicates.
    """
    chunks: list[Chunk] = chunk_segments(raw_segments)
    if not chunks:
        return 0

    existing = await _existing_chunk_count(db, meeting_id)
    new_chunks = [c for c in chunks if c.index >= existing]
    if not new_chunks:
        return 0

    embedder = embedder or GeminiEmbedder()
    vectors = await embedder.embed_documents([c.text for c in new_chunks])

    rows = [
        {
            "meeting_id": meeting_id,
            "chunk_index": chunk.index,
            "text": chunk.text,
            "speaker": chunk.speaker or None,
            "start_time": chunk.start_time,
            "end_time": chunk.end_time,
            "char_count": chunk.char_count,
            "embedding": vector,
            "embed_model": embedder._model,  # noqa: SLF001 - record provenance
        }
        for chunk, vector in zip(new_chunks, vectors, strict=True)
    ]
    stmt = pg_insert(TranscriptChunk).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["meeting_id", "chunk_index"])
    await db.execute(stmt)
    await db.commit()
    log.info(
        "copilot_indexed_chunks",
        meeting_id=meeting_id,
        added=len(rows),
        total=existing + len(rows),
    )
    return len(rows)


async def retrieve_context(
    db: AsyncSession,
    meeting_id: int,
    query: str,
    *,
    top_k: int,
    embedder: GeminiEmbedder | None = None,
) -> list[RetrievedChunk]:
    """Return the top-K transcript chunks most similar to ``query`` (cosine)."""
    embedder = embedder or GeminiEmbedder()
    query_vector = await embedder.embed_query(query)

    distance = TranscriptChunk.embedding.cosine_distance(query_vector)
    result = await db.execute(
        select(
            TranscriptChunk.id,
            TranscriptChunk.chunk_index,
            TranscriptChunk.text,
            TranscriptChunk.speaker,
            distance.label("distance"),
        )
        .where(TranscriptChunk.meeting_id == meeting_id)
        .order_by(distance)
        .limit(top_k)
    )
    chunks = [
        RetrievedChunk(
            id=row.id,
            chunk_index=row.chunk_index,
            text=row.text,
            speaker=row.speaker,
            distance=float(row.distance),
        )
        for row in result.all()
    ]
    log.info(
        "copilot_retrieved",
        meeting_id=meeting_id,
        query_chars=len(query),
        returned=len(chunks),
    )
    return chunks
