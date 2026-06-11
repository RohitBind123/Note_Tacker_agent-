"""Transcript chunker for the copilot retrieval layer.

Groups consecutive transcript segments into speaker-labelled chunks of a bounded
character size. Chunks are the unit we embed + retrieve, so they need to be:

  - Small enough that a top-K retrieval returns focused context (not a whole
    meeting), but large enough to carry a coherent thought across a few turns.
  - Deterministic from the start of the transcript, so re-chunking a GROWN
    transcript reproduces the earlier chunks byte-for-byte. That stability is
    what lets the store layer embed only NEW chunks (idempotent on
    ``(meeting_id, chunk_index)``) instead of re-embedding everything each poll.

Pure functions only — no I/O — so the boundary logic is unit-testable without a
live meeting.
"""
from __future__ import annotations

from dataclasses import dataclass

# Target/max chunk size in characters. A chunk closes once adding the next
# segment would exceed TARGET; a single oversized segment becomes its own chunk.
TARGET_CHARS = 700
MAX_CHARS = 1200


@dataclass(frozen=True)
class TranscriptSegment:
    """A normalised transcript segment (defensive over Vexa field spellings)."""

    speaker: str
    text: str
    start_time: float | None
    end_time: float | None


@dataclass(frozen=True)
class Chunk:
    """A contiguous, speaker-labelled window of transcript ready to embed."""

    index: int
    text: str
    speaker: str
    start_time: float | None
    end_time: float | None

    @property
    def char_count(self) -> int:
        return len(self.text)


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_segment(raw: dict) -> TranscriptSegment | None:
    """Map a raw Vexa segment dict to a TranscriptSegment, or None if empty."""
    if not isinstance(raw, dict):
        return None
    speaker = str(
        raw.get("speaker") or raw.get("speaker_name") or raw.get("participant") or ""
    ).strip()
    text = str(raw.get("text") or raw.get("content") or "").strip()
    if not text:
        return None
    start = _coerce_float(
        raw.get("start") if raw.get("start") is not None else raw.get("start_time")
    )
    end = _coerce_float(
        raw.get("end") if raw.get("end") is not None else raw.get("end_time")
    )
    return TranscriptSegment(speaker=speaker, text=text, start_time=start, end_time=end)


def _segment_line(segment: TranscriptSegment) -> str:
    return f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text


def chunk_segments(raw_segments: list[dict]) -> list[Chunk]:
    """Group raw transcript segments into bounded, speaker-labelled chunks.

    Boundaries are decided purely by accumulated character count, so the output
    is a deterministic function of the input prefix: chunk ``i`` depends only on
    segments up to where it closes, never on later ones.
    """
    segments = [s for s in (normalize_segment(r) for r in raw_segments) if s is not None]
    chunks: list[Chunk] = []

    buffer: list[TranscriptSegment] = []
    buffer_chars = 0

    def flush() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        text = "\n".join(_segment_line(s) for s in buffer)
        speakers = [s.speaker for s in buffer if s.speaker]
        # Single dominant speaker if uniform, else a compact "A, B" label.
        unique = list(dict.fromkeys(speakers))
        speaker_label = unique[0] if len(unique) == 1 else ", ".join(unique)
        chunks.append(
            Chunk(
                index=len(chunks),
                text=text,
                speaker=speaker_label,
                start_time=buffer[0].start_time,
                end_time=buffer[-1].end_time,
            )
        )
        buffer = []
        buffer_chars = 0

    for segment in segments:
        line_len = len(_segment_line(segment))
        # Close the current chunk before adding this segment if it would push us
        # past TARGET (and we already have content). A lone oversized segment is
        # allowed through up to MAX as its own chunk.
        if buffer and buffer_chars + line_len > TARGET_CHARS:
            flush()
        buffer.append(segment)
        buffer_chars += line_len + 1  # +1 for the join newline
        if buffer_chars >= MAX_CHARS:
            flush()

    flush()
    return chunks
