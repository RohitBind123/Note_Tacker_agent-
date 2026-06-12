"""Scheduler — dispatch bots at meeting time and advance the lifecycle.

Two passes per tick:
  1. dispatch_due  — claim SCHEDULED meetings whose start time is near, set them
     JOINING (claim-lock with FOR UPDATE SKIP LOCKED so two workers never grab
     the same row), then call the provider OUTSIDE the lock (no network I/O in a
     held transaction).
  2. advance_active — poll JOINING/ACTIVE meetings; when the bot leaves, move to
     PROCESSING and capture the transcript. (Gemini + email land in P4/P5.)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Meeting, MeetingStatus
from app.db.session import async_session_factory
from app.logging_config import get_logger
from app.services import orchestrator
from app.services.meeting_dedup import dedupe_claims_by_native
from app.services.vexa.factory import get_provider

log = get_logger(__name__)

# Don't dispatch a bot for a meeting that started more than this long ago.
_STALE_AFTER = timedelta(minutes=30)
# A meeting claimed (JOINING) but never given a bot id -> a crash between claim
# and dispatch. Reclaim it after this long.
_CLAIM_TIMEOUT = timedelta(minutes=5)
# A dispatched bot should never run longer than this; if it does, force-process.
_ACTIVE_TIMEOUT = timedelta(hours=3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def end_reason(
    meeting: Meeting,
    now: datetime,
    *,
    grace_seconds: int,
    hard_timeout: timedelta,
) -> str | None:
    """Why an in-progress meeting should be force-ended now (or None to keep it).

    Pure and side-effect-free so it is unit-testable. Used to stop a bot that is
    lingering in a Meet past when the meeting should be over — the reliable
    signals are the calendar's scheduled end_time and an absolute hard cap.
    (Vexa cloud's participants_count is unreliable — it reports 0 even with a
    human present — so it is intentionally NOT used here.)
    """
    if meeting.end_time is not None and now >= meeting.end_time + timedelta(seconds=grace_seconds):
        return "past_end_time"
    if meeting.bot_dispatched_at is not None and now >= meeting.bot_dispatched_at + hard_timeout:
        return "hard_timeout"
    return None


def dispatch_window_missed(
    meeting: Meeting, now: datetime, *, stale_after: timedelta
) -> bool:
    """True if a not-yet-dispatched meeting's start time is too far past to join.

    Mirrors the lower bound of ``_claim_due``: that pass only claims meetings whose
    scheduled start is within the last ``stale_after``. Once start_time is older
    than that, the dispatcher will NEVER pick the meeting up, so a SCHEDULED/PENDING
    row that still has no bot is unrecoverable and must be retired to a terminal
    state instead of hanging in SCHEDULED forever. Pure / side-effect-free so it is
    unit-testable; the status and "no bot yet" filters live in the SQL query.

    Boundary matches ``_claim_due`` exactly: that pass claims ``start_time >= now -
    stale_after`` (boundary still claimable), so a meeting is "missed" only when
    strictly older, i.e. ``now > start_time + stale_after``.
    """
    return meeting.start_time is not None and now > meeting.start_time + stale_after


async def _claim_due(db: AsyncSession) -> list[int]:
    """Atomically claim due SCHEDULED meetings -> JOINING. Returns claimed ids."""
    now = _now()
    due_by = now + timedelta(seconds=settings.dispatch_lead_seconds)
    stale_cutoff = now - _STALE_AFTER

    stmt = (
        select(Meeting)
        .where(
            Meeting.status == MeetingStatus.SCHEDULED,
            Meeting.start_time.is_not(None),
            Meeting.start_time <= due_by,
            Meeting.start_time >= stale_cutoff,
        )
        .with_for_update(skip_locked=True)
        .limit(10)
    )
    rows = (await db.execute(stmt)).scalars().all()
    claims: list[tuple[int, str]] = []
    for m in rows:
        m.status = MeetingStatus.JOINING
        m.dispatch_claimed_at = now
        claims.append((m.id, m.native_meeting_id))

    # Defence-in-depth: if two rows for the SAME Meet code were claimed in one
    # tick, dispatch only the lowest id and CANCEL the rest so two bots never
    # enter one room. uq_meetings_active_native makes two in-flight rows for one
    # code near-impossible post-index; this also covers the brief pre-index
    # window and is pure-tested (dedupe_claims_by_native).
    dispatch_ids, cancel_ids = dedupe_claims_by_native(claims)
    if cancel_ids:
        cancel_set = set(cancel_ids)
        for m in rows:
            if m.id in cancel_set:
                m.status = MeetingStatus.CANCELLED
                m.dispatch_claimed_at = None  # clear the phantom claim
                m.failure_reason = (
                    "duplicate in-flight meeting for the same Meet code "
                    "(deduped at claim)"
                )
        log.warning(
            "scheduler_claim_deduped", cancelled=cancel_ids, dispatched=dispatch_ids
        )
    await db.commit()
    if dispatch_ids:
        log.info("scheduler_claimed", meeting_ids=dispatch_ids)
    return dispatch_ids


async def dispatch_due() -> None:
    provider = get_provider()
    async with async_session_factory() as db:
        claimed = await _claim_due(db)

    for meeting_id in claimed:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                continue
            try:
                await orchestrator.dispatch_existing(db, meeting, provider=provider)
            except Exception:
                log.exception("scheduler_dispatch_error", meeting_id=meeting_id)


async def advance_active() -> None:
    provider = get_provider()
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Meeting).where(
                    Meeting.status.in_([MeetingStatus.JOINING, MeetingStatus.ACTIVE]),
                    Meeting.vexa_bot_id.is_not(None),
                )
            )
        ).scalars().all()
        active = [m.id for m in rows]

    grace = settings.meeting_end_grace_seconds
    for meeting_id in active:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                continue
            try:
                meeting = await orchestrator.refresh_status(db, meeting, provider=provider)
                # If the provider still reports the bot in-flight, force-stop it
                # once the meeting should be over so it never lingers in the Meet.
                if meeting.status in (MeetingStatus.JOINING, MeetingStatus.ACTIVE):
                    reason = end_reason(
                        meeting, _now(), grace_seconds=grace, hard_timeout=_ACTIVE_TIMEOUT
                    )
                    if reason:
                        log.info("auto_stopping_meeting", meeting_id=meeting_id, reason=reason)
                        await orchestrator.stop_meeting(db, meeting, provider=provider)
                # Finalization (transcript -> insights -> email) is handled by
                # process_pending, the single convergence point for ended meetings.
            except Exception:
                log.exception("scheduler_advance_error", meeting_id=meeting_id)


async def process_pending() -> None:
    """Finalize ended meetings -> transcript -> insights -> email.

    The single place that runs the insight pipeline, regardless of HOW the
    meeting ended (provider reported it gone, user hit stop, or end_time passed).
    Idempotent per meeting via orchestrator.finalize_meeting.

    Also retries EMAIL_FAILED meetings whose attempt count is still under
    settings.email_max_attempts, so a transient SMTP failure no longer strands a
    meeting with no insight email forever (the send is re-claimed atomically, so
    a retry never double-sends).
    """
    provider = get_provider()
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Meeting).where(
                    or_(
                        Meeting.status == MeetingStatus.PROCESSING,
                        and_(
                            Meeting.status == MeetingStatus.EMAIL_FAILED,
                            func.coalesce(Meeting.email_attempts, 0)
                            < settings.email_max_attempts,
                        ),
                    )
                )
            )
        ).scalars().all()
        pending = [m.id for m in rows]

    for meeting_id in pending:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                continue
            try:
                await orchestrator.finalize_meeting(db, meeting, provider=provider)
            except Exception:
                log.exception("scheduler_process_error", meeting_id=meeting_id)


async def recover_stale() -> None:
    """Reclaim meetings stuck by a mid-flight crash so they never hang forever."""
    now = _now()
    async with async_session_factory() as db:
        # Claimed (JOINING) but never dispatched -> back to SCHEDULED to retry.
        stuck = (
            await db.execute(
                select(Meeting).where(
                    Meeting.status == MeetingStatus.JOINING,
                    Meeting.vexa_bot_id.is_(None),
                    Meeting.dispatch_claimed_at.is_not(None),
                    Meeting.dispatch_claimed_at < now - _CLAIM_TIMEOUT,
                )
            )
        ).scalars().all()
        for m in stuck:
            m.status = MeetingStatus.SCHEDULED
            m.dispatch_claimed_at = None
            log.warning("recovered_stuck_claim", meeting_id=m.id)

        # Dispatched but running absurdly long -> force into processing.
        ancient = (
            await db.execute(
                select(Meeting).where(
                    Meeting.status.in_([MeetingStatus.JOINING, MeetingStatus.ACTIVE]),
                    Meeting.bot_dispatched_at.is_not(None),
                    Meeting.bot_dispatched_at < now - _ACTIVE_TIMEOUT,
                )
            )
        ).scalars().all()
        for m in ancient:
            m.status = MeetingStatus.PROCESSING
            m.end_time = m.end_time or now
            log.warning("recovered_ancient_active", meeting_id=m.id)

        # SCHEDULED/PENDING whose dispatch window irrecoverably passed (start_time
        # older than _STALE_AFTER, so _claim_due will never claim it) and which
        # never received a bot -> retire to a terminal state so it cannot hang in
        # SCHEDULED forever. recover_stale runs before dispatch_due in the same
        # tick and the two boundaries align (dispatch claims >= now-_STALE_AFTER,
        # this fails < now-_STALE_AFTER), so no meeting is both retired and
        # dispatched in one tick.
        missed = (
            await db.execute(
                select(Meeting).where(
                    Meeting.status.in_([MeetingStatus.SCHEDULED, MeetingStatus.PENDING]),
                    Meeting.vexa_bot_id.is_(None),
                    Meeting.start_time.is_not(None),
                    Meeting.start_time < now - _STALE_AFTER,
                )
            )
        ).scalars().all()
        for m in missed:
            m.status = MeetingStatus.FAILED_JOIN
            m.failure_reason = (
                "missed dispatch window: scheduled start passed by more than "
                f"{int(_STALE_AFTER.total_seconds() // 60)} minutes before a bot "
                "could be dispatched"
            )
            log.warning(
                "recovered_missed_window",
                meeting_id=m.id,
                start_time=m.start_time.isoformat(),
            )

        if stuck or ancient or missed:
            await db.commit()


async def tick() -> None:
    """One scheduler iteration (recover + dispatch + advance + finalize)."""
    await recover_stale()
    await dispatch_due()
    await advance_active()
    await process_pending()
