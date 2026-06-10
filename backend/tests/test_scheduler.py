"""Unit tests for the scheduler's pure decision logic.

The DB-driven passes (dispatch_due / advance_active / process_pending) are
verified by real e2e against the deployed instance; here we lock down the pure
``end_reason`` helper that decides when a lingering bot must be force-stopped.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.scheduler import _ACTIVE_TIMEOUT, end_reason

UTC = timezone.utc
GRACE = 120


def _meeting(*, end_time=None, bot_dispatched_at=None):
    return SimpleNamespace(end_time=end_time, bot_dispatched_at=bot_dispatched_at)


def test_keeps_meeting_running_within_time():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(
        end_time=now + timedelta(minutes=10),
        bot_dispatched_at=now - timedelta(minutes=5),
    )
    assert end_reason(m, now, grace_seconds=GRACE, hard_timeout=_ACTIVE_TIMEOUT) is None


def test_within_grace_after_end_time_still_runs():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    # ended 60s ago; grace is 120s -> not yet eligible to stop
    m = _meeting(end_time=now - timedelta(seconds=60))
    assert end_reason(m, now, grace_seconds=GRACE, hard_timeout=_ACTIVE_TIMEOUT) is None


def test_past_end_time_plus_grace_stops():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(end_time=now - timedelta(seconds=GRACE + 1))
    assert end_reason(m, now, grace_seconds=GRACE, hard_timeout=_ACTIVE_TIMEOUT) == "past_end_time"


def test_hard_timeout_backstop_when_no_end_time():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(bot_dispatched_at=now - _ACTIVE_TIMEOUT - timedelta(minutes=1))
    assert end_reason(m, now, grace_seconds=GRACE, hard_timeout=_ACTIVE_TIMEOUT) == "hard_timeout"


def test_no_end_time_and_recent_dispatch_keeps_running():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(bot_dispatched_at=now - timedelta(minutes=30))
    assert end_reason(m, now, grace_seconds=GRACE, hard_timeout=_ACTIVE_TIMEOUT) is None
