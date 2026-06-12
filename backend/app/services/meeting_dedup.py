"""Pure helpers for the 'one live row per Meet room' invariant.

The true business identity of a meeting is the Google Meet room code
(``native_meeting_id``, e.g. ``abc-defg-hij``) — a live room cannot host two
concurrent bot sessions. The detection sources key on their own ids
(``google_event_id`` for the calendar poller, ``gmail_message_id`` for the Gmail
scanner), so neither alone prevents two rows for one room. The DB partial unique
index ``uq_meetings_active_native`` (one in-flight row per code) is the hard
backstop; these pure helpers let the app skip conflicts *gracefully* (clean logs,
no IntegrityError churn) and are unit-testable without a database.

See ARCHITECTURE.md "Idempotency & Exactly-Once Guarantees".
"""
from __future__ import annotations


def partition_calendar_candidates(
    candidates: list[dict],
    inflight_by_native: dict[str, set[str | None]],
) -> tuple[list[dict], list[dict]]:
    """Split calendar-poller candidate rows into ``(keep, skip)``.

    A candidate is SKIPPED when its Meet code (``native_meeting_id``) is already
    in flight under a *different* source identity — an existing non-terminal row
    whose ``google_event_id`` differs from this candidate's. A gmail-sourced row
    holds ``None``, which always differs, so it skips too (the symmetric partner
    to the Gmail scanner's existing calendar-side guard).

    Refreshing the SAME event (its own ``google_event_id`` is the only in-flight
    holder of the code) is KEPT: the upsert merely updates that row's metadata.

    Order-preserving and pure so it is testable without a DB.
    """
    keep: list[dict] = []
    skip: list[dict] = []
    for row in candidates:
        native = row["native_meeting_id"]
        event_id = row.get("google_event_id")
        holders = inflight_by_native.get(native)
        # KEEP when the code has no in-flight holder, OR this exact event is
        # already a holder (the upsert just refreshes its own row — no new row).
        # SKIP only when a *different* source holds the code, because inserting
        # this candidate would create a SECOND in-flight row for one Meet room.
        # (Checking ``event_id in holders`` — not "all holders are me" — keeps a
        # legitimate self-refresh even in the should-be-impossible case where the
        # code briefly has multiple holders.)
        if holders and event_id not in holders:
            skip.append(row)
        else:
            keep.append(row)
    return keep, skip


def dedupe_claims_by_native(
    claims: list[tuple[int, str]],
) -> tuple[list[int], list[int]]:
    """Dedupe meetings claimed in one scheduler tick by Meet code.

    Given ``(meeting_id, native_meeting_id)`` pairs for the rows a single
    ``_claim_due`` pass flipped to JOINING, return ``(dispatch_ids, cancel_ids)``:
    keep the lowest meeting id per code (deterministic), cancel every other row
    sharing that code so two bots never enter one room from one batch.

    Defence-in-depth alongside ``uq_meetings_active_native``: post-index two
    SCHEDULED rows for one code cannot coexist, so this normally cancels nothing,
    but it also covers the brief pre-index window and any future widening of the
    claimable status set. Pure / testable.
    """
    seen: dict[str, int] = {}
    dispatch: list[int] = []
    cancel: list[int] = []
    for meeting_id, native in sorted(claims):  # ascending id -> lowest id wins
        if native in seen:
            cancel.append(meeting_id)
        else:
            seen[native] = meeting_id
            dispatch.append(meeting_id)
    return dispatch, cancel
