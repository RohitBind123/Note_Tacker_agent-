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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Meeting, MeetingStatus
from app.db.session import async_session_factory
from app.logging_config import get_logger
from app.services import orchestrator
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
    claimed: list[int] = []
    for m in rows:
        m.status = MeetingStatus.JOINING
        m.dispatch_claimed_at = now
        claimed.append(m.id)
    await db.commit()
    if claimed:
        log.info("scheduler_claimed", meeting_ids=claimed)
    return claimed


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

    for meeting_id in active:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                continue
            try:
                meeting = await orchestrator.refresh_status(db, meeting, provider=provider)
                if meeting.status == MeetingStatus.PROCESSING:
                    # Meeting just ended -> capture transcript, then analyze.
                    await orchestrator.fetch_and_store_transcript(db, meeting, provider=provider)
                    try:
                        await orchestrator.run_analysis(db, meeting)
                        log.info("scheduler_meeting_analyzed", meeting_id=meeting_id)
                    except Exception:
                        meeting.status = MeetingStatus.FAILED_ANALYSIS
                        await db.commit()
                        log.exception("scheduler_analysis_error", meeting_id=meeting_id)
                        continue
                    try:
                        await orchestrator.send_report_email(db, meeting)
                        log.info("scheduler_meeting_completed", meeting_id=meeting_id)
                    except Exception:
                        log.exception("scheduler_email_error", meeting_id=meeting_id)
            except Exception:
                log.exception("scheduler_advance_error", meeting_id=meeting_id)


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

        if stuck or ancient:
            await db.commit()


async def tick() -> None:
    """One scheduler iteration (recover + dispatch + advance)."""
    await recover_stale()
    await dispatch_due()
    await advance_active()
