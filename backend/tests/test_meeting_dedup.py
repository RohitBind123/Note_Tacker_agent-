"""Unit tests for the pure 'one live row per Meet room' decision helpers.

These back the structural duplicate-bot prevention: the calendar poller and the
scheduler both must avoid creating / dispatching a second in-flight row for the
same native_meeting_id (Google Meet room code). The DB partial unique index
``uq_meetings_active_native`` is the hard backstop; these pure helpers let the
app skip conflicts gracefully and are testable without a DB (mirrors the
pure-logic approach in test_scheduler.py).
"""
from __future__ import annotations

from app.services.meeting_dedup import (
    dedupe_claims_by_native,
    partition_calendar_candidates,
)

# --------------------------------------------------------------------------- #
# partition_calendar_candidates                                               #
# --------------------------------------------------------------------------- #


def _cand(event_id: str, native: str) -> dict:
    return {"google_event_id": event_id, "native_meeting_id": native}


def test_keeps_all_when_nothing_in_flight():
    rows = [_cand("g1", "abc-defg-hij"), _cand("g2", "klm-nopq-rst")]
    keep, skip = partition_calendar_candidates(rows, {})
    assert keep == rows
    assert skip == []


def test_keeps_same_event_refresh():
    # The SAME calendar event is already in flight (its own google_event_id holds
    # the code). Upserting it just refreshes metadata -> keep, no duplicate.
    rows = [_cand("g1", "abc-defg-hij")]
    inflight = {"abc-defg-hij": {"g1"}}
    keep, skip = partition_calendar_candidates(rows, inflight)
    assert keep == rows
    assert skip == []


def test_skips_when_other_calendar_event_holds_code():
    # A DIFFERENT calendar event already holds this Meet code in flight ->
    # inserting this one would put a second bot in the same room. Skip it.
    rows = [_cand("g2", "abc-defg-hij")]
    inflight = {"abc-defg-hij": {"g1"}}
    keep, skip = partition_calendar_candidates(rows, inflight)
    assert keep == []
    assert skip == rows


def test_skips_when_gmail_sourced_row_holds_code():
    # A gmail-sourced row (google_event_id is None) already holds the code ->
    # None always differs from the candidate's event id -> skip (symmetric guard:
    # the gmail scanner already checks the calendar; this is the other direction).
    rows = [_cand("g1", "abc-defg-hij")]
    inflight = {"abc-defg-hij": {None}}
    keep, skip = partition_calendar_candidates(rows, inflight)
    assert keep == []
    assert skip == rows


def test_multi_holder_keeps_self_refresh_skips_others():
    # Should be impossible post-index, but if a code briefly has >1 in-flight
    # holder, a candidate that is ITSELF a holder must still be kept (refresh its
    # own row), while a candidate that is not a holder must be skipped.
    inflight = {"abc-defg-hij": {"g1", "g2"}}
    keep, skip = partition_calendar_candidates([_cand("g1", "abc-defg-hij")], inflight)
    assert keep == [_cand("g1", "abc-defg-hij")]
    assert skip == []

    keep, skip = partition_calendar_candidates([_cand("g3", "abc-defg-hij")], inflight)
    assert keep == []
    assert skip == [_cand("g3", "abc-defg-hij")]


def test_mixed_batch_partitions_correctly():
    rows = [
        _cand("g1", "aaa-aaaa-aaa"),  # fresh -> keep
        _cand("g2", "bbb-bbbb-bbb"),  # held by other event -> skip
        _cand("g3", "ccc-cccc-ccc"),  # held by itself (refresh) -> keep
    ]
    inflight = {"bbb-bbbb-bbb": {"gX"}, "ccc-cccc-ccc": {"g3"}}
    keep, skip = partition_calendar_candidates(rows, inflight)
    assert keep == [rows[0], rows[2]]
    assert skip == [rows[1]]


# --------------------------------------------------------------------------- #
# dedupe_claims_by_native                                                     #
# --------------------------------------------------------------------------- #


def test_distinct_codes_all_dispatch():
    claims = [(5, "aaa-aaaa-aaa"), (7, "bbb-bbbb-bbb")]
    dispatch, cancel = dedupe_claims_by_native(claims)
    assert sorted(dispatch) == [5, 7]
    assert cancel == []


def test_same_code_keeps_lowest_id_cancels_rest():
    # Two rows for the same Meet code claimed in one tick -> dispatch the lowest
    # id, cancel the other so two bots never enter one room.
    claims = [(9, "abc-defg-hij"), (4, "abc-defg-hij"), (12, "abc-defg-hij")]
    dispatch, cancel = dedupe_claims_by_native(claims)
    assert dispatch == [4]
    assert sorted(cancel) == [9, 12]


def test_mixed_dedupe():
    claims = [(3, "dup"), (1, "dup"), (2, "uniq")]
    dispatch, cancel = dedupe_claims_by_native(claims)
    assert sorted(dispatch) == [1, 2]
    assert cancel == [3]


def test_empty_claims():
    dispatch, cancel = dedupe_claims_by_native([])
    assert dispatch == []
    assert cancel == []
