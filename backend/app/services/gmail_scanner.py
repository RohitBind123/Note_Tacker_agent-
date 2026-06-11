"""Gmail invite scanner — detect Meet invites sent directly to the bot's inbox.

Complements the calendar poller, which only sees meetings created via Google
Calendar. When someone creates a meeting from meet.google.com and uses
"Add people" to invite centralagentai@gmail.com, Google sends an email but
creates no Calendar event. This scanner reads those emails and upserts meetings
rows so the scheduler can dispatch the bot normally.

Design mirrors calendar_poller.poll_once:
- Idempotent: scanning the same email twice produces exactly one meetings row
  (keyed on gmail_message_id, enforced by a partial unique index).
- Never touches status / vexa fields on existing rows.
- google_event_id stays NULL for all Gmail-sourced rows.
- Controlled by GMAIL_SCAN_ENABLED (default False) — safe to ship before the
  gmail.readonly OAuth scope is granted.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Meeting, MeetingStatus
from app.logging_config import get_logger
from app.services.gmail.invite_parser import parse as parse_invite
from app.services.gmail.reader import GmailReader

log = get_logger(__name__)


async def scan_once(
    db: AsyncSession,
    *,
    reader: GmailReader | None = None,
    now: datetime | None = None,
) -> int:
    """Run a single Gmail scan; returns the number of meetings upserted.

    Args:
        db:     SQLAlchemy async session (request-scoped for writes).
        reader: Injectable GmailReader (defaults to a real one; override in tests).
        now:    Injectable "current time" (defaults to UTC now; override in tests).
    """
    if not settings.gmail_scan_enabled:
        log.info("gmail_scan_disabled")
        return 0

    reader = reader or GmailReader()
    now = now or datetime.now(timezone.utc)

    # 1. List candidate message IDs from Gmail.
    message_ids = await reader.list_message_ids(
        settings.gmail_scan_query,
        max_results=settings.gmail_scan_max_results,
    )
    if not message_ids:
        log.info("gmail_scan_no_candidates")
        return 0

    # 2. Fast-path dedup: skip IDs already in the DB (avoids downloading bodies
    #    for messages we've already processed — saves Gmail API quota).
    existing_result = await db.execute(
        select(Meeting.gmail_message_id).where(
            Meeting.gmail_message_id.in_(message_ids)
        )
    )
    already_known = {row[0] for row in existing_result.fetchall()}
    new_ids = [mid for mid in message_ids if mid not in already_known]
    if not new_ids:
        log.info("gmail_scan_all_known", total=len(message_ids))
        return 0

    # 3. Fetch + parse each new message.
    rows: list[dict] = []
    for mid in new_ids:
        try:
            msg = await reader.get_message(mid)
        except Exception:
            log.exception("gmail_scan_fetch_error", message_id=mid)
            continue

        parsed = parse_invite(
            subject=msg.subject,
            from_addr=msg.from_addr,
            body_text=msg.body_text,
        )
        if parsed is None:
            log.info("gmail_scan_skip_no_meet", message_id=mid, subject=msg.subject[:80])
            continue

        # For instant meets (no scheduled time in the email), treat start_time
        # as "now" — this places the row inside _claim_due's dispatch window
        # (start_time <= now + dispatch_lead_seconds), so the scheduler's next
        # tick (every ~20s) will claim and dispatch the bot immediately.
        start_time = parsed.start_time or now

        rows.append(
            {
                "gmail_message_id": mid,
                "platform": "google_meet",
                "native_meeting_id": parsed.native_meeting_id,
                "meet_url": parsed.meet_url,
                "title": parsed.title or "Google Meet (email invite)",
                "organizer_email": parsed.organizer_email,
                "attendees": None,
                "start_time": start_time,
                "end_time": parsed.end_time,
                "status": MeetingStatus.SCHEDULED,
            }
        )

    if not rows:
        log.info("gmail_scan_no_actionable", checked=len(new_ids))
        return 0

    # 4. Upsert — idempotent on gmail_message_id.
    #    On conflict (same Gmail message ID seen again), refresh metadata but
    #    NEVER touch status / vexa_bot_id — identical invariant to calendar_poller.
    stmt = pg_insert(Meeting).values(rows)
    stmt = stmt.on_conflict_do_update(
        # Reference the partial index by name — the most robust form for
        # partial indexes; avoids SQLAlchemy dialect quirks around column
        # objects + index_where that can produce a "no matching constraint"
        # error at runtime when no column-level unique constraint exists.
        constraint="uq_meetings_gmail_message_id",
        set_={
            "native_meeting_id": stmt.excluded.native_meeting_id,
            "meet_url": stmt.excluded.meet_url,
            "title": stmt.excluded.title,
            "organizer_email": stmt.excluded.organizer_email,
            "start_time": stmt.excluded.start_time,
            "end_time": stmt.excluded.end_time,
        },
    )
    await db.execute(stmt)
    await db.commit()
    log.info("gmail_scan_upserted", count=len(rows))
    return len(rows)
