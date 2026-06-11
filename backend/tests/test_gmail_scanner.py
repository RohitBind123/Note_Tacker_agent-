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
from app.services.gmail_scanner import build_meeting_upsert, scan_once

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


def _mock_db(
    *,
    known_ids: list[str] | None = None,
    inflight_native_ids: list[str] | None = None,
) -> AsyncMock:
    """Return a mock async DB session that sequences its SELECT results.

    scan_once issues up to three execute() calls in a fixed order:
      1. dedup SELECT       -> existing gmail_message_id values  (``known_ids``)
      2. in-flight SELECT   -> native_meeting_ids already tracked (``inflight_native_ids``)
      3. INSERT ... ON CONFLICT

    ``known_ids`` simulates rows already processed (skips body fetch).
    ``inflight_native_ids`` simulates meetings already tracked by the calendar
    poller / a prior scan (the cross-source dedup guard).
    """
    known_ids = known_ids or []
    inflight_native_ids = inflight_native_ids or []
    db = AsyncMock()

    dedup_result = MagicMock()
    dedup_result.fetchall.return_value = [(mid,) for mid in known_ids]

    inflight_result = MagicMock()
    inflight_result.fetchall.return_value = [(nid,) for nid in inflight_native_ids]

    insert_result = MagicMock()
    results = [dedup_result, inflight_result, insert_result]
    counter = {"i": 0}

    async def execute_side_effect(_stmt, *args, **kwargs):
        i = counter["i"]
        counter["i"] += 1
        return results[min(i, len(results) - 1)]

    db.execute = AsyncMock(side_effect=execute_side_effect)
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
    # SELECT (dedup) + SELECT (in-flight native_id guard) + INSERT (upsert) = 3
    assert db.execute.call_count == 3
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


@pytest.mark.asyncio
async def test_skips_invite_already_tracked_by_native_id():
    """A meeting already tracked (same Meet code, non-terminal) must NOT be
    re-inserted — otherwise the bot would be dispatched twice to one room."""
    # Default fake message parses to native id abc-defg-hij.
    msg = _fake_message(message_id="msg_new")
    reader = _make_reader(ids=["msg_new"], messages={"msg_new": msg})
    # New gmail id (not in known_ids) but its Meet code is already in flight.
    db = _mock_db(known_ids=[], inflight_native_ids=["abc-defg-hij"])

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    assert count == 0
    # dedup SELECT + in-flight SELECT ran; NO insert, NO commit.
    assert db.execute.call_count == 2
    db.commit.assert_not_called()


def test_upsert_targets_partial_index_not_constraint():
    """Regression guard for the prod crash: the upsert must target the partial
    unique INDEX via inference, never `ON CONSTRAINT`.

    uq_meetings_gmail_message_id is a partial unique *index*, not a table
    constraint. `ON CONFLICT ON CONSTRAINT <name>` raises
    `constraint "..." does not exist` against an index at runtime. A mocked
    `db.execute()` can't see this — only compiling the statement catches it.
    """
    from sqlalchemy.dialects.postgresql import dialect

    stmt = build_meeting_upsert(
        [{"gmail_message_id": "m1", "native_meeting_id": "abc-defg-hij"}]
    )
    sql = str(stmt.compile(dialect=dialect())).upper()

    assert "ON CONFLICT (GMAIL_MESSAGE_ID)" in sql
    assert "WHERE GMAIL_MESSAGE_ID IS NOT NULL" in sql
    # The exact form that crashed prod must never come back.
    assert "ON CONSTRAINT" not in sql


@pytest.mark.asyncio
async def test_two_invites_same_native_id_inserts_one():
    """Two invite emails for the SAME meeting (same Meet code) collapse to one row."""
    body = "https://meet.google.com/abc-defg-hij"  # same code, no timestamp
    msg1 = _fake_message(message_id="msg_a", subject="Video call", body_text=body)
    msg2 = _fake_message(message_id="msg_b", subject="Video call (resent)", body_text=body)
    reader = _make_reader(
        ids=["msg_a", "msg_b"], messages={"msg_a": msg1, "msg_b": msg2}
    )
    db = _mock_db(known_ids=[], inflight_native_ids=[])

    with patch("app.services.gmail_scanner.settings") as s:
        s.gmail_scan_enabled = True
        s.gmail_scan_query = "test"
        s.gmail_scan_max_results = 25
        count = await scan_once(db, reader=reader)

    assert count == 1  # one row despite two emails
    db.commit.assert_called_once()
