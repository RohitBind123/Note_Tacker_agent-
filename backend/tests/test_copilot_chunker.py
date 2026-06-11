"""Unit tests for the transcript chunker (Batch 3).

The chunker is the foundation of idempotent indexing: it MUST be a deterministic
function of the transcript prefix, so a grown transcript reproduces the earlier
chunks unchanged. These tests pin that determinism + the boundary/labelling
behaviour.
"""
from app.services.copilot.chunker import (
    MAX_CHARS,
    TARGET_CHARS,
    chunk_segments,
    normalize_segment,
)


def _seg(speaker: str, text: str, start=None, end=None) -> dict:
    return {"speaker": speaker, "text": text, "start": start, "end": end}


def test_empty_transcript_yields_no_chunks():
    assert chunk_segments([]) == []


def test_blank_and_malformed_segments_are_dropped():
    segs = [
        {"speaker": "A", "text": "   "},  # whitespace only
        {"speaker": "B"},  # no text
        "not a dict",  # wrong type
        {"speaker": "C", "text": "real content here"},
    ]
    chunks = chunk_segments(segs)
    assert len(chunks) == 1
    assert chunks[0].text == "C: real content here"


def test_short_segments_collapse_into_one_chunk():
    segs = [_seg("Alice", "hello"), _seg("Bob", "hi there"), _seg("Alice", "lets begin")]
    chunks = chunk_segments(segs)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].text == "Alice: hello\nBob: hi there\nAlice: lets begin"
    # mixed speakers -> compact joined label, de-duplicated, order-preserving
    assert chunks[0].speaker == "Alice, Bob"


def test_uniform_speaker_label_is_single_name():
    segs = [_seg("Alice", "one"), _seg("Alice", "two")]
    assert chunk_segments(segs)[0].speaker == "Alice"


def test_chunks_split_on_target_size():
    # Each line ~ "Spkr: " + 100 chars. Several of them exceed TARGET -> multiple chunks.
    line = "x" * 100
    segs = [_seg("S", line) for _ in range(20)]
    chunks = chunk_segments(segs)
    assert len(chunks) >= 2
    # indices are contiguous from 0
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # every chunk respects the MAX ceiling
    assert all(c.char_count <= MAX_CHARS for c in chunks)


def test_char_count_matches_text_length():
    segs = [_seg("Alice", "hello world")]
    chunk = chunk_segments(segs)[0]
    assert chunk.char_count == len(chunk.text) == len("Alice: hello world")


def test_determinism_growing_transcript_preserves_earlier_chunks():
    # The key idempotency invariant: chunking a longer transcript must reproduce
    # the chunks of its prefix exactly (same index, same text).
    line = "y" * 120
    base = [_seg("S", line) for _ in range(10)]
    grown = base + [_seg("S", line) for _ in range(10)]

    base_chunks = chunk_segments(base)
    grown_chunks = chunk_segments(grown)

    assert len(grown_chunks) >= len(base_chunks)
    for early, later in zip(base_chunks, grown_chunks):
        assert early.index == later.index
        assert early.text == later.text


def test_start_end_times_span_the_chunk():
    segs = [
        _seg("A", "first", start=1.0, end=2.0),
        _seg("A", "second", start=2.0, end=3.5),
    ]
    chunk = chunk_segments(segs)[0]
    assert chunk.start_time == 1.0
    assert chunk.end_time == 3.5


def test_normalize_segment_reads_alternate_field_names():
    seg = normalize_segment(
        {"participant": "Sam", "content": "alt fields", "start_time": 5, "end_time": 9}
    )
    assert seg is not None
    assert seg.speaker == "Sam"
    assert seg.text == "alt fields"
    assert seg.start_time == 5.0 and seg.end_time == 9.0


def test_normalize_segment_returns_none_for_empty_text():
    assert normalize_segment({"speaker": "A", "text": ""}) is None
    assert normalize_segment("nope") is None


def test_target_below_max_invariant():
    assert TARGET_CHARS < MAX_CHARS
