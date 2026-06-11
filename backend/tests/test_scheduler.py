"""Unit tests for the scheduler's pure decision logic.

The DB-driven passes (dispatch_due / advance_active / process_pending) are
verified by real e2e against the deployed instance; here we lock down the pure
``end_reason`` helper that decides when a lingering bot must be force-stopped.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.scheduler import (
    _ACTIVE_TIMEOUT,
    _STALE_AFTER,
    dispatch_window_missed,
    end_reason,
)

UTC = timezone.utc
GRACE = 120


def _meeting(*, end_time=None, bot_dispatched_at=None, start_time=None):
    return SimpleNamespace(
        end_time=end_time, bot_dispatched_at=bot_dispatched_at, start_time=start_time
    )


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


# --- dispatch_window_missed: retire SCHEDULED meetings the dispatcher can't claim ---


def test_missed_window_true_when_start_long_past():
    # start older than _STALE_AFTER -> _claim_due can never claim it -> retire it.
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(start_time=now - _STALE_AFTER - timedelta(minutes=1))
    assert dispatch_window_missed(m, now, stale_after=_STALE_AFTER) is True


def test_missed_window_false_when_recently_started():
    # within the claim window -> dispatcher will still send a bot, don't retire.
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(start_time=now - timedelta(minutes=5))
    assert dispatch_window_missed(m, now, stale_after=_STALE_AFTER) is False


def test_missed_window_false_when_start_in_future():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(start_time=now + timedelta(minutes=5))
    assert dispatch_window_missed(m, now, stale_after=_STALE_AFTER) is False


def test_missed_window_false_when_no_start_time():
    # PENDING rows with no start_time yet are left for the poller to schedule.
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(start_time=None)
    assert dispatch_window_missed(m, now, stale_after=_STALE_AFTER) is False


def test_missed_window_boundary_is_still_claimable():
    # exactly at the boundary is still claimable (dispatch uses >= now-stale_after),
    # so it must NOT be retired -> dispatch_window_missed is False at the boundary.
    now = datetime(2026, 6, 11, 10, 0, tzinfo=UTC)
    m = _meeting(start_time=now - _STALE_AFTER)
    assert dispatch_window_missed(m, now, stale_after=_STALE_AFTER) is False
