"""Unit tests for the calendar poller's cross-source in-flight guard.

Mocked DB + fake CalendarClient (no network / no DB), mirroring the pure-logic
approach in test_gmail_scanner.py. The DB-level upsert + partial unique index are
verified by the real e2e flow; here we lock down the symmetric guard that stops
the poller creating a SECOND in-flight row for a Meet code already tracked under
a different source identity.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.calendar_poller import poll_once
from app.services.google.calendar import CalendarEvent

UTC = timezone.utc


def _event(
    *,
    event_id: str = "g1",
    meet_url: str = "https://meet.google.com/abc-defg-hij",
    self_response_status: str = "accepted",
) -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        title="Sync",
        start=datetime(2026, 6, 12, 10, 0, tzinfo=UTC),
        end=datetime(2026, 6, 12, 10, 30, tzinfo=UTC),
        meet_url=meet_url,
        organizer_email="organizer@example.com",
        attendees=["organizer@example.com"],
        self_response_status=self_response_status,
        raw={},
    )


def _make_client(events: list[CalendarEvent]) -> AsyncMock:
    client = AsyncMock()
    client.list_upcoming_meet_events = AsyncMock(return_value=events)
    client.accept_invite = AsyncMock()
    return client


def _mock_db(inflight: list[tuple[str, str | None]] | None = None) -> AsyncMock:
    """Sequence the poller's execute() calls.

    poll_once issues, in order:
      1. in-flight SELECT  -> [(native_meeting_id, google_event_id), ...] via .all()
      2. INSERT ... ON CONFLICT (only when at least one candidate survives)
    """
    inflight = inflight or []
    db = AsyncMock()

    inflight_result = MagicMock()
    inflight_result.all.return_value = list(inflight)
    insert_result = MagicMock()

    results = [inflight_result, insert_result]
    counter = {"i": 0}

    async def execute_side_effect(_stmt, *args, **kwargs):
        i = counter["i"]
        counter["i"] += 1
        return results[min(i, len(results) - 1)]

    db.execute = AsyncMock(side_effect=execute_side_effect)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_upserts_new_event_when_nothing_in_flight():
    client = _make_client([_event(event_id="g1")])
    db = _mock_db(inflight=[])

    count = await poll_once(db, client=client)

    assert count == 1
    # in-flight SELECT + INSERT
    assert db.execute.call_count == 2
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_skips_event_whose_code_is_inflight_under_other_source():
    # Same Meet code already in flight under a DIFFERENT calendar event id ->
    # inserting this one would dispatch a second bot to the same room. Skip it.
    client = _make_client([_event(event_id="g2")])  # parses to abc-defg-hij
    db = _mock_db(inflight=[("abc-defg-hij", "g1")])

    count = await poll_once(db, client=client)

    assert count == 0
    # Only the in-flight SELECT ran; NO insert, NO commit.
    assert db.execute.call_count == 1
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_skips_event_whose_code_is_inflight_under_gmail_row():
    # A gmail-sourced in-flight row holds the code (google_event_id is None).
    client = _make_client([_event(event_id="g1")])
    db = _mock_db(inflight=[("abc-defg-hij", None)])

    count = await poll_once(db, client=client)

    assert count == 0
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_keeps_same_event_refresh():
    # The SAME event already in flight under its OWN id -> upsert refreshes it.
    client = _make_client([_event(event_id="g1")])
    db = _mock_db(inflight=[("abc-defg-hij", "g1")])

    count = await poll_once(db, client=client)

    assert count == 1
    assert db.execute.call_count == 2
    db.commit.assert_called_once()
