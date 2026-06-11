"""Unit tests for the chat-capture dedup key (Batch 6).

The persist + mention-routing round-trip is DB-driven and validated in the
real-meeting E2E batch; here we pin the idempotency anchor itself — the
dedup_key derived from (sender, timestamp, text).
"""
from app.services.copilot.capture import compute_dedup_key


def test_dedup_key_is_deterministic():
    a = compute_dedup_key("Priya", "1718000000", "@centralagent recap?")
    b = compute_dedup_key("Priya", "1718000000", "@centralagent recap?")
    assert a == b


def test_dedup_key_is_64_hex_chars():
    key = compute_dedup_key("Priya", "1718000000", "hello")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_dedup_key_distinguishes_each_field():
    base = compute_dedup_key("Priya", "1718000000", "hello")
    assert compute_dedup_key("Sam", "1718000000", "hello") != base       # sender
    assert compute_dedup_key("Priya", "1718000001", "hello") != base     # timestamp
    assert compute_dedup_key("Priya", "1718000000", "hello!") != base    # text


def test_dedup_key_is_none_safe():
    # Missing sender/timestamp must not raise; they collapse to empty fields.
    key = compute_dedup_key(None, None, "just text")
    assert len(key) == 64
    # Same text with explicit empties hashes identically (stable normalization).
    assert key == compute_dedup_key("", "", "just text")
