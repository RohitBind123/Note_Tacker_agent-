"""Calendar poller — detect meetings the bot was invited to.

Reads the bot's calendar on an interval and upserts each Meet-bearing event into
``meetings`` (idempotent on ``google_event_id``). New rows start as SCHEDULED;
existing rows have their metadata refreshed but their lifecycle ``status`` is
preserved (so we never re-dispatch a meeting already in flight).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Meeting, MeetingStatus
from app.logging_config import get_logger
from app.services.google.calendar import CalendarClient
from app.services.meet_url import InvalidMeetUrl, parse_native_meeting_id

log = get_logger(__name__)


async def poll_once(
    db: AsyncSession,
    *,
    client: CalendarClient | None = None,
    look_back: timedelta = timedelta(minutes=15),
    look_ahead: timedelta = timedelta(hours=24),
) -> int:
    """Run a single poll; returns the number of events upserted."""
    client = client or CalendarClient()
    now = datetime.now(timezone.utc)
    events = await client.list_upcoming_meet_events(
        time_min=now - look_back, time_max=now + look_ahead
    )

    # Auto-RSVP "yes" to invitations the bot hasn't responded to yet.
    for ev in events:
        if ev.self_response_status == "needsAction" and ev.raw.get("attendees"):
            try:
                await client.accept_invite(ev.event_id, ev.raw["attendees"])
            except Exception:
                log.warning("poller_rsvp_error", event_id=ev.event_id)

    rows: list[dict] = []
    for ev in events:
        try:
            native_id = parse_native_meeting_id(ev.meet_url or "")
        except InvalidMeetUrl:
            log.warning("poller_skip_bad_meet_url", event_id=ev.event_id, meet_url=ev.meet_url)
            continue
        rows.append(
            {
                "google_event_id": ev.event_id,
                "platform": "google_meet",
                "native_meeting_id": native_id,
                "meet_url": ev.meet_url,
                "title": ev.title,
                "organizer_email": ev.organizer_email,
                "start_time": ev.start,
                "end_time": ev.end,
                "status": MeetingStatus.SCHEDULED,
            }
        )

    if not rows:
        log.info("poller_no_actionable_events")
        return 0

    stmt = pg_insert(Meeting).values(rows)
    # On conflict, refresh metadata but NEVER touch status / vexa fields.
    stmt = stmt.on_conflict_do_update(
        index_elements=[Meeting.google_event_id],
        set_={
            "native_meeting_id": stmt.excluded.native_meeting_id,
            "meet_url": stmt.excluded.meet_url,
            "title": stmt.excluded.title,
            "organizer_email": stmt.excluded.organizer_email,
            "start_time": stmt.excluded.start_time,
            "end_time": stmt.excluded.end_time,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()
    log.info("poller_upserted", count=len(rows))
    return len(rows)
