"""Unit tests for Vexa webhook verification + envelope parsing (Batch 6).

The security-critical core: HMAC over ``f"{timestamp}.".encode() + raw_body``,
constant-time compare, replay window, and defensive envelope extraction. The DB
finalize path the endpoint drives is validated in the real-meeting E2E batch.
"""
import pytest

from app.services.copilot.webhook import (
    EVENT_MEETING_COMPLETED,
    compute_signature,
    is_fresh_timestamp,
    parse_webhook_event,
    verify_signature,
)

SECRET = "whsec_test_abc123"
TS = "1718000000"
BODY = b'{"event_id":"evt_1","event_type":"meeting.completed"}'


# --- signature ---------------------------------------------------------------


def test_compute_signature_is_deterministic_and_prefixed():
    sig1 = compute_signature(SECRET, TS, BODY)
    sig2 = compute_signature(SECRET, TS, BODY)
    assert sig1 == sig2
    assert sig1.startswith("sha256=")
    assert len(sig1) == len("sha256=") + 64  # hex sha256


def test_signature_binds_timestamp_and_body():
    base = compute_signature(SECRET, TS, BODY)
    # Changing the timestamp changes the signature (replay binding)...
    assert compute_signature(SECRET, "1718000001", BODY) != base
    # ...and so does changing a single body byte (tamper detection).
    assert compute_signature(SECRET, TS, BODY + b" ") != base
    # ...and so does the secret.
    assert compute_signature("other", TS, BODY) != base


def test_verify_accepts_a_correct_signature():
    sig = compute_signature(SECRET, TS, BODY)
    assert verify_signature(SECRET, TS, BODY, sig) is True


def test_verify_rejects_wrong_secret_or_tampered_body():
    sig = compute_signature(SECRET, TS, BODY)
    assert verify_signature("wrong", TS, BODY, sig) is False
    assert verify_signature(SECRET, TS, BODY + b"x", sig) is False
    assert verify_signature(SECRET, "1718000099", BODY, sig) is False


@pytest.mark.parametrize(
    "secret,ts,sig",
    [
        ("", TS, "sha256=deadbeef"),   # no secret -> fail closed
        (SECRET, None, "sha256=deadbeef"),  # no timestamp
        (SECRET, TS, None),           # no signature header
        (SECRET, TS, ""),
    ],
)
def test_verify_fails_closed_on_missing_pieces(secret, ts, sig):
    assert verify_signature(secret, ts, BODY, sig) is False


# --- timestamp freshness -----------------------------------------------------


def test_fresh_timestamp_within_window():
    now = 1718000000.0
    assert is_fresh_timestamp("1718000000", now) is True
    assert is_fresh_timestamp("1717999800", now, max_skew_seconds=300) is True  # 200s old
    assert is_fresh_timestamp("1718000200", now, max_skew_seconds=300) is True  # 200s ahead


def test_stale_timestamp_rejected():
    now = 1718000000.0
    assert is_fresh_timestamp("1717999000", now, max_skew_seconds=300) is False  # 1000s old


@pytest.mark.parametrize("bad", [None, "", "not-a-number", "  "])
def test_unparseable_timestamp_rejected(bad):
    assert is_fresh_timestamp(bad, 1718000000.0) is False


# --- envelope parsing --------------------------------------------------------


def test_parse_meeting_completed_event():
    body = {
        "event_id": "evt_42",
        "event_type": EVENT_MEETING_COMPLETED,
        "api_version": "2026-03-01",
        "data": {"meeting": {"platform": "google_meet", "native_meeting_id": "abc-defg-hij"}},
    }
    evt = parse_webhook_event(body)
    assert evt is not None
    assert evt.event_id == "evt_42"
    assert evt.is_meeting_completed is True
    assert evt.platform == "google_meet"
    assert evt.native_meeting_id == "abc-defg-hij"


def test_parse_tolerates_field_aliases():
    body = {
        "event_id": "evt_7",
        "event_type": "meeting.completed",
        "data": {"meeting": {"platform_name": "google_meet", "native_id": "xyz-1234-abc"}},
    }
    evt = parse_webhook_event(body)
    assert evt.platform == "google_meet"
    assert evt.native_meeting_id == "xyz-1234-abc"


def test_parse_returns_none_without_event_type():
    assert parse_webhook_event({"data": {}}) is None
    assert parse_webhook_event({}) is None
    assert parse_webhook_event("not a dict") is None


def test_parse_non_completed_event_is_not_flagged_completed():
    evt = parse_webhook_event({"event_id": "e", "event_type": "meeting.started"})
    assert evt is not None
    assert evt.is_meeting_completed is False
    assert evt.native_meeting_id is None
