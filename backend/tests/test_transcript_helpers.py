from app.services.vexa.cloud_provider import _extract_segments, _segments_to_text


def test_extract_segments_variants():
    assert _extract_segments({"segments": [{"text": "hi"}]}) == [{"text": "hi"}]
    assert _extract_segments({"transcripts": [{"text": "a"}]}) == [{"text": "a"}]
    assert _extract_segments([{"text": "x"}]) == [{"text": "x"}]
    assert _extract_segments({"nothing": 1}) == []


def test_segments_to_text_with_speakers():
    segs = [
        {"speaker": "John", "text": "Backend is deployed"},
        {"speaker": "Jane", "text": "Testing tomorrow"},
    ]
    assert _segments_to_text(segs) == "John: Backend is deployed\nJane: Testing tomorrow"


def test_segments_to_text_skips_empty_and_handles_missing_speaker():
    segs = [{"text": "no speaker line"}, {"speaker": "X", "text": ""}, "junk"]
    assert _segments_to_text(segs) == "no speaker line"
