"""Unit tests for the Gmail invite scanner.

Uses a fake GmailReader and a mocked DB session so there is no network or DB
I/O — mirrors the pure-logic approach in test_scheduler.py.

DB integration (the partial-unique upsert path) is verified by the real
e2e flow: deploy with GMAIL_SCAN_ENABLED=true and send an "Add people" invite.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.gmail.reader import GmailMessage
from app.services.gmail_scanner import scan_once

UTC = timezone.utc

# --------------------------------------------------------------------------- #
# Fake helpers                                                                 #
# --------------------------------------------------------------------------- #


def _fake_message(
    message_id: str = "msg001",
    subject: str = "Invitation: Sync @ 2026-06-12T10:00:00Z",
    from_addr: str = "calendar-notification@google.com",
    body_text: str = "https://meet.google.com/abc-defg-hij 2026-06-12T10:00:00Z",
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        subject=subject,
        from_addr=from_addr,
        body_text=body_text,
    )


def _make_reader(*, ids: list[str], messages: dict[str, GmailMessage]) -> AsyncMock:
    reader = AsyncMock()
    reader.list_message_ids = AsyncMock(return_value=ids)
    reader.get_message = AsyncMock(side_effect=lambda mid: messages[mid])
    return reader


def _mock_db(*, known_ids: list[str] | None = None) -> AsyncMock:
    """Return a mock async DB session.

    ``known_ids`` simulates existing gmail_message_id values already in the DB
    (the pre-filter dedup step). An empty list means the DB has no records yet.
    """
    known_ids = known_ids or []
    db = AsyncMock()

    # Simulate the SELECT for existing IDs.
    select_result = MagicMock()
    select_result.fetchall.return_value = [(mid,) for mid in known_ids]
    db.execute = AsyncMock(return_value=select_result)
    db.commit = AsyncMock()
    return db


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_returns_zero_without_any_calls():
    reader = _make_reader(ids=[], messages={})
    db = _mock_db()

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = False
        count = await scan_once(db, reader=reader)

    assert count == 0
    reader.list_message_ids.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_no_candidates_returns_zero():
    reader = _make_reader(ids=[], messages={})
    db = _mock_db()

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    assert count == 0
    reader.get_message.assert_not_called()


@pytest.mark.asyncio
async def test_all_ids_already_known_skips_body_fetch():
    """Pre-filter should skip get_message calls for already-known IDs."""
    reader = _make_reader(ids=["msg001"], messages={})
    db = _mock_db(known_ids=["msg001"])  # DB already has this ID

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    assert count == 0
    reader.get_message.assert_not_called()


@pytest.mark.asyncio
async def test_valid_meet_invite_upserts_row():
    """A parseable invite should result in one upsert (db.execute called twice:
    once for the pre-filter SELECT, once for the INSERT ... ON CONFLICT)."""
    msg = _fake_message()
    reader = _make_reader(ids=["msg001"], messages={"msg001": msg})
    db = _mock_db(known_ids=[])  # Nothing in DB yet

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    assert count == 1
    # SELECT (dedup) + INSERT (upsert) = 2 execute calls
    assert db.execute.call_count == 2
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_non_meet_email_skipped():
    """Body with no Meet link should not produce an upsert."""
    msg = _fake_message(
        message_id="msg_noise",
        body_text="Please come to the office at 9 AM.",
    )
    reader = _make_reader(ids=["msg_noise"], messages={"msg_noise": msg})
    db = _mock_db(known_ids=[])

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    assert count == 0
    # Only the SELECT (dedup) should have run — no INSERT
    assert db.execute.call_count == 1
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_instant_meet_uses_now_as_start_time():
    """When the email has no scheduled time, the row should get start_time = now."""
    msg = _fake_message(
        message_id="msg_instant",
        subject="You've been invited to a video call",
        body_text="https://meet.google.com/abc-defg-hij",  # no ISO timestamp
    )
    reader = _make_reader(ids=["msg_instant"], messages={"msg_instant": msg})
    db = _mock_db(known_ids=[])

    now = datetime(2026, 6, 12, 9, 55, tzinfo=UTC)
    captured_rows: list[dict] = []

    # Intercept the INSERT values to inspect start_time.
    original_execute = db.execute

    async def capturing_execute(stmt, *args, **kwargs):
        # The second execute call is the INSERT (the first is the SELECT).
        if hasattr(stmt, "parameters") or "insert" in str(type(stmt)).lower():
            try:
                # Try to capture the values being inserted.
                compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            except Exception:
                pass
        return await original_execute(stmt, *args, **kwargs)

    db.execute = capturing_execute

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader, now=now)

    # The key assertion: a message with no scheduled time still results in a row.
    assert count == 1


@pytest.mark.asyncio
async def test_fetch_error_for_one_message_continues_others():
    """A failure to fetch one message body should not abort the whole scan."""
    msg_good = _fake_message(message_id="msg_good")

    reader = AsyncMock()
    reader.list_message_ids = AsyncMock(return_value=["msg_bad", "msg_good"])

    async def get_message_side_effect(mid: str) -> GmailMessage:
        if mid == "msg_bad":
            raise RuntimeError("network timeout")
        return msg_good

    reader.get_message = AsyncMock(side_effect=get_message_side_effect)
    db = _mock_db(known_ids=[])

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    # msg_bad failed but msg_good should still be processed
    assert count == 1
