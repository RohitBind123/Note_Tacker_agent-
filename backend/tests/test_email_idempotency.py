"""Unit tests for exactly-once insight email (the atomic email claim).

Mocked DB (no network / no DB), mirroring test_gmail_scanner.py. The real
UPDATE ... RETURNING is exercised by e2e; here we lock down the behaviour that
guarantees a report is emailed at most once and that a send failure releases the
claim and bumps the bounded retry counter.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import MeetingStatus
from app.services import orchestrator


def _meeting() -> SimpleNamespace:
    return SimpleNamespace(
        id=5,
        organizer_email="organizer@example.com",
        attendees=None,
        status=MeetingStatus.PROCESSING,
        email_attempts=0,
        failure_reason=None,
    )


def _mock_db(*, report, claimed_id) -> AsyncMock:
    """Sequence send_report_email's execute() calls.

    Order: (1) SELECT report -> scalar_one_or_none = report,
           (2) UPDATE ... RETURNING email claim -> scalar_one_or_none = claimed_id,
           (3) [failure path only] release UPDATE.
    """
    db = AsyncMock()

    report_result = MagicMock()
    report_result.scalar_one_or_none.return_value = report
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = claimed_id
    release_result = MagicMock()

    results = [report_result, claim_result, release_result]
    counter = {"i": 0}

    async def execute_side_effect(_stmt, *args, **kwargs):
        i = counter["i"]
        counter["i"] += 1
        return results[min(i, len(results) - 1)]

    db.execute = AsyncMock(side_effect=execute_side_effect)
    db.commit = AsyncMock()
    return db


def _patches(send_mock):
    """Common patches: sender, email template, and settings."""
    s = MagicMock()
    s.email_recipients = "organizer"
    s.bot_google_email = "bot@example.com"
    s.report_fallback_email = ""
    tmpl = MagicMock()
    tmpl.build_subject.return_value = "subject"
    tmpl.build_html.return_value = "<html></html>"
    return (
        patch("app.services.orchestrator.send_html_email", new=send_mock),
        patch("app.services.orchestrator.email_template", new=tmpl),
        patch("app.services.orchestrator.settings", new=s),
    )


@pytest.mark.asyncio
async def test_first_send_sends_once_and_completes():
    meeting = _meeting()
    db = _mock_db(report=MagicMock(), claimed_id=5)  # claim won
    send_mock = AsyncMock(return_value="msg-1")
    p1, p2, p3 = _patches(send_mock)

    with p1, p2, p3:
        result = await _run(db, meeting)

    assert result == "msg-1"
    send_mock.assert_awaited_once()
    assert meeting.status == MeetingStatus.COMPLETED


@pytest.mark.asyncio
async def test_second_send_does_not_resend():
    meeting = _meeting()
    db = _mock_db(report=MagicMock(), claimed_id=None)  # claim lost (already sent)
    send_mock = AsyncMock(return_value="msg-X")
    p1, p2, p3 = _patches(send_mock)

    with p1, p2, p3:
        result = await _run(db, meeting)

    assert result == ""  # no new send
    send_mock.assert_not_called()
    assert meeting.status == MeetingStatus.COMPLETED


@pytest.mark.asyncio
async def test_send_failure_releases_claim_and_bumps_attempts():
    meeting = _meeting()
    db = _mock_db(report=MagicMock(), claimed_id=5)  # claim won, but send fails
    send_mock = AsyncMock(side_effect=RuntimeError("smtp down"))
    p1, p2, p3 = _patches(send_mock)

    with p1, p2, p3:
        with pytest.raises(RuntimeError):
            await _run(db, meeting)

    assert meeting.status == MeetingStatus.EMAIL_FAILED
    assert meeting.email_attempts == 1
    # report SELECT + claim UPDATE + release UPDATE = 3 execute calls
    assert db.execute.call_count == 3


async def _run(db, meeting):
    return await orchestrator.send_report_email(db, meeting)
