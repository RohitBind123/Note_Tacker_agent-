"""Meeting orchestration — dispatch a bot, track its status, capture transcript.

P2 covers the manual path (given a Meet URL). The scheduler (P3) reuses these
same functions once meetings come from the calendar.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Meeting, MeetingReport, MeetingStatus, Transcript
from app.logging_config import get_logger
from app.services import email_template
from app.services.gemini.analyzer import GeminiAnalyzer
from app.services.gmail.sender import send_html_email
from app.services.meet_url import build_meet_url, parse_native_meeting_id
from app.services.vexa.factory import get_provider
from app.services.vexa.provider import BotProvider

log = get_logger(__name__)

# Map Vexa's raw status strings onto our domain state machine.
_VEXA_TO_STATUS = {
    "requested": MeetingStatus.JOINING,
    "joining": MeetingStatus.JOINING,
    "awaiting_admission": MeetingStatus.JOINING,
    "active": MeetingStatus.ACTIVE,
    "processing": MeetingStatus.PROCESSING,
    "completed": MeetingStatus.COMPLETED,
    "stopped": MeetingStatus.PROCESSING,
    "failed": MeetingStatus.FAILED_JOIN,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def dispatch_by_url(
    db: AsyncSession,
    meet_url: str,
    *,
    title: str | None = None,
    organizer_email: str | None = None,
    bot_name: str = "CentralAgent Notetaker",
    provider: BotProvider | None = None,
) -> Meeting:
    """Create a meeting row and send a bot to the given Meet URL."""
    native_id = parse_native_meeting_id(meet_url)
    provider = provider or get_provider()

    meeting = Meeting(
        platform="google_meet",
        native_meeting_id=native_id,
        meet_url=build_meet_url(native_id),
        title=title,
        organizer_email=organizer_email,
        status=MeetingStatus.JOINING,
    )
    db.add(meeting)
    await db.flush()  # assign id before the external call
    log.info("dispatch_created_meeting", meeting_id=meeting.id, native_meeting_id=native_id)

    try:
        result = await provider.join(native_id, bot_name=bot_name)
    except Exception as exc:
        meeting.status = MeetingStatus.FAILED_JOIN
        meeting.failure_reason = f"join failed: {exc}"
        await db.commit()
        log.error("dispatch_join_failed", meeting_id=meeting.id, error=str(exc))
        raise

    meeting.vexa_bot_id = result.vexa_bot_id
    meeting.bot_dispatched_at = _now()
    meeting.status = _VEXA_TO_STATUS.get(result.status, MeetingStatus.JOINING)
    await db.commit()
    await db.refresh(meeting)
    log.info(
        "dispatch_ok",
        meeting_id=meeting.id,
        vexa_bot_id=meeting.vexa_bot_id,
        status=meeting.status.value,
    )
    return meeting


async def dispatch_existing(
    db: AsyncSession,
    meeting: Meeting,
    *,
    bot_name: str = "CentralAgent Notetaker",
    provider: BotProvider | None = None,
) -> Meeting:
    """Send a bot for a meeting row that already exists (e.g. from the calendar)."""
    provider = provider or get_provider()
    log.info(
        "dispatch_existing_start",
        meeting_id=meeting.id,
        native_meeting_id=meeting.native_meeting_id,
    )
    try:
        result = await provider.join(meeting.native_meeting_id, bot_name=bot_name)
    except Exception as exc:
        meeting.status = MeetingStatus.FAILED_JOIN
        meeting.failure_reason = f"join failed: {exc}"
        await db.commit()
        log.error("dispatch_existing_failed", meeting_id=meeting.id, error=str(exc))
        raise

    meeting.vexa_bot_id = result.vexa_bot_id
    meeting.bot_dispatched_at = _now()
    meeting.status = _VEXA_TO_STATUS.get(result.status, MeetingStatus.JOINING)
    await db.commit()
    await db.refresh(meeting)
    log.info("dispatch_existing_ok", meeting_id=meeting.id, status=meeting.status.value)
    return meeting


async def refresh_status(
    db: AsyncSession, meeting: Meeting, *, provider: BotProvider | None = None
) -> Meeting:
    """Poll the provider once and reconcile our row's status."""
    provider = provider or get_provider()
    status = await provider.get_status(meeting.native_meeting_id)

    if status is None:
        # Bot no longer active. If it was in-flight, the call has ended -> process.
        if meeting.status in (MeetingStatus.JOINING, MeetingStatus.ACTIVE):
            meeting.status = MeetingStatus.PROCESSING
            meeting.end_time = meeting.end_time or _now()
            log.info("refresh_meeting_ended", meeting_id=meeting.id)
    else:
        mapped = _VEXA_TO_STATUS.get(status.status)
        if mapped and mapped != meeting.status:
            log.info(
                "refresh_status_change",
                meeting_id=meeting.id,
                from_status=meeting.status.value,
                to_status=mapped.value,
            )
            meeting.status = mapped
    await db.commit()
    await db.refresh(meeting)
    return meeting


async def fetch_and_store_transcript(
    db: AsyncSession, meeting: Meeting, *, provider: BotProvider | None = None
) -> Transcript:
    """Fetch the transcript from the provider and persist it (raw preserved)."""
    provider = provider or get_provider()
    result = await provider.get_transcript(meeting.native_meeting_id)

    existing = (
        await db.execute(select(Transcript).where(Transcript.meeting_id == meeting.id))
    ).scalar_one_or_none()
    if existing:
        existing.segments = result.segments
        existing.full_text = result.full_text
        existing.fetched_at = _now()
        transcript = existing
    else:
        transcript = Transcript(
            meeting_id=meeting.id,
            segments=result.segments,
            full_text=result.full_text,
            source="vexa_cloud",
            fetched_at=_now(),
        )
        db.add(transcript)

    await db.commit()
    await db.refresh(transcript)
    log.info(
        "transcript_stored",
        meeting_id=meeting.id,
        segments=len(result.segments),
        chars=len(result.full_text),
    )
    return transcript


async def run_analysis(
    db: AsyncSession, meeting: Meeting, *, analyzer: GeminiAnalyzer | None = None
) -> MeetingReport:
    """Analyze the stored transcript with Gemini and persist the report."""
    analyzer = analyzer or GeminiAnalyzer()
    transcript = (
        await db.execute(select(Transcript).where(Transcript.meeting_id == meeting.id))
    ).scalar_one_or_none()
    if transcript is None:
        raise RuntimeError(f"no transcript stored for meeting {meeting.id}")

    data = await analyzer.analyze(transcript.full_text or "")

    report = (
        await db.execute(select(MeetingReport).where(MeetingReport.meeting_id == meeting.id))
    ).scalar_one_or_none()
    if report is None:
        report = MeetingReport(meeting_id=meeting.id)
        db.add(report)

    report.summary = data.get("summary")
    report.decisions = data.get("decisions")
    report.action_items = data.get("action_items")
    report.risks = data.get("risks")
    report.next_steps = data.get("next_steps")
    report.model_used = settings.gemini_model

    await db.commit()
    await db.refresh(report)
    log.info("analysis_stored", meeting_id=meeting.id, model=report.model_used)
    return report


async def send_report_email(db: AsyncSession, meeting: Meeting) -> str:
    """Email the insights report to the organizer; marks meeting COMPLETED."""
    report = (
        await db.execute(select(MeetingReport).where(MeetingReport.meeting_id == meeting.id))
    ).scalar_one_or_none()
    if report is None:
        raise RuntimeError(f"no report to email for meeting {meeting.id}")

    to = meeting.organizer_email
    if not to:
        meeting.status = MeetingStatus.EMAIL_FAILED
        meeting.failure_reason = "no organizer email on meeting"
        await db.commit()
        raise RuntimeError(f"meeting {meeting.id} has no organizer email")

    subject = email_template.build_subject(meeting)
    html = email_template.build_html(meeting, report)
    try:
        message_id = await send_html_email(to=to, subject=subject, html=html)
    except Exception as exc:
        meeting.status = MeetingStatus.EMAIL_FAILED
        meeting.failure_reason = f"email failed: {exc}"
        await db.commit()
        log.error("report_email_failed", meeting_id=meeting.id, error=str(exc))
        raise

    report.email_sent_at = _now()
    meeting.status = MeetingStatus.COMPLETED
    await db.commit()
    log.info("report_emailed", meeting_id=meeting.id, to=to, message_id=message_id)
    return message_id
