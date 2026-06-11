"""Unit tests for the Vexa WS wire-format helpers (Batch 2).

The reconnecting socket loop is integration-tested against a live meeting in a
later batch; here we pin the pure parser + subscribe-payload builder, which is
where the real risk (envelope-shape drift) lives.
"""
import json

from app.services.vexa.ws_client import (
    build_subscribe_payload,
    parse_ws_event,
)


def test_parse_enveloped_chat_event():
    evt = parse_ws_event({
        "type": "chat.received",
        "meeting": {"platform": "google_meet", "native_id": "abc-defg-hij"},
        "data": {"sender": "Priya", "text": "@centralagent recap?", "timestamp": 123},
    })
    assert evt is not None
    assert evt.is_chat and not evt.is_transcript
    assert evt.platform == "google_meet"
    assert evt.native_id == "abc-defg-hij"
    msg = evt.as_chat_message()
    assert msg is not None
    assert msg.sender == "Priya"
    assert msg.text == "@centralagent recap?"
    assert msg.timestamp == "123"


def test_parse_flat_chat_event():
    # Flat envelope: routing + payload at the top level, no nested "data".
    evt = parse_ws_event({
        "event": "chat.received",
        "platform": "google_meet",
        "native_meeting_id": "abc-defg-hij",
        "sender": "Sam",
        "text": "hello",
    })
    assert evt is not None and evt.is_chat
    assert evt.native_id == "abc-defg-hij"
    msg = evt.as_chat_message()
    assert msg is not None and msg.sender == "Sam" and msg.text == "hello"


def test_parse_transcript_event_is_not_chat():
    evt = parse_ws_event({"type": "transcript.mutable", "data": {"text": "blah"}})
    assert evt is not None
    assert evt.is_transcript and not evt.is_chat
    assert evt.as_chat_message() is None


def test_parse_from_json_string():
    raw = json.dumps({"type": "chat.received", "data": {"sender": "X", "text": "y"}})
    evt = parse_ws_event(raw)
    assert evt is not None and evt.as_chat_message().text == "y"


def test_parse_control_frame_returns_none():
    # Subscribe ack / ping with no event type -> ignored.
    assert parse_ws_event({"action": "subscribed", "ok": True}) is None
    assert parse_ws_event("not json") is None
    assert parse_ws_event({"data": {}}) is None


def test_chat_event_without_text_yields_no_message():
    evt = parse_ws_event({"type": "chat.received", "data": {"sender": "X"}})
    assert evt is not None and evt.as_chat_message() is None


def test_build_subscribe_payload():
    frame = build_subscribe_payload([("google_meet", "abc-defg-hij")])
    obj = json.loads(frame)
    assert obj["action"] == "subscribe"
    assert obj["meetings"] == [{"platform": "google_meet", "native_id": "abc-defg-hij"}]


def test_build_unsubscribe_payload():
    frame = build_subscribe_payload([("google_meet", "x")], action="unsubscribe")
    assert json.loads(frame)["action"] == "unsubscribe"
