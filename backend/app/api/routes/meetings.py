"""Meeting endpoints — manual dispatch + read/status/transcript (P2).

These are internal/debug-facing (per decision: Gmail is the only user-facing
delivery). They let us drive and observe the bot lifecycle end-to-end.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Meeting, MeetingReport, Transcript
from app.db.session import get_db
from app.logging_config import get_logger
from app.schemas.meetings import (
    DispatchRequest,
    EmailResult,
    MeetingOut,
    ReportOut,
    StopResult,
    TranscriptOut,
)
from app.services.meet_url import InvalidMeetUrl
from app.services import orchestrator
from app.services.vexa.factory import get_provider

router = APIRouter(prefix="/meetings", tags=["meetings"])
log = get_logger(__name__)


async def _get_meeting_or_404(db: AsyncSession, meeting_id: int) -> Meeting:
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")
    return meeting


@router.post("/dispatch", response_model=MeetingOut, status_code=201)
async def dispatch_meeting(body: DispatchRequest, db: AsyncSession = Depends(get_db)) -> Meeting:
    try:
        return await orchestrator.dispatch_by_url(
            db,
            body.meet_url,
            title=body.title,
            organizer_email=body.organizer_email,
            bot_name=body.bot_name,
        )
    except InvalidMeetUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[MeetingOut])
async def list_meetings(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Meeting]:
    rows = await db.execute(
        select(Meeting).order_by(Meeting.id.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all())


@router.get("/{meeting_id}", response_model=MeetingOut)
async def get_meeting(meeting_id: int, db: AsyncSession = Depends(get_db)) -> Meeting:
    return await _get_meeting_or_404(db, meeting_id)


@router.post("/{meeting_id}/refresh", response_model=MeetingOut)
async def refresh_meeting(meeting_id: int, db: AsyncSession = Depends(get_db)) -> Meeting:
    meeting = await _get_meeting_or_404(db, meeting_id)
    return await orchestrator.refresh_status(db, meeting)


@router.post("/{meeting_id}/transcript", response_model=TranscriptOut)
async def fetch_transcript(meeting_id: int, db: AsyncSession = Depends(get_db)) -> TranscriptOut:
    meeting = await _get_meeting_or_404(db, meeting_id)
    transcript = await orchestrator.fetch_and_store_transcript(db, meeting)
    return TranscriptOut(
        meeting_id=meeting.id,
        segment_count=len(transcript.segments or []),
        full_text=transcript.full_text or "",
        source=transcript.source,
        fetched_at=transcript.fetched_at,
    )


@router.get("/{meeting_id}/transcript", response_model=TranscriptOut)
async def get_transcript(meeting_id: int, db: AsyncSession = Depends(get_db)) -> TranscriptOut:
    row = await db.execute(select(Transcript).where(Transcript.meeting_id == meeting_id))
    transcript = row.scalar_one_or_none()
    if transcript is None:
        raise HTTPException(status_code=404, detail="transcript not captured yet")
    return TranscriptOut(
        meeting_id=meeting_id,
        segment_count=len(transcript.segments or []),
        full_text=transcript.full_text or "",
        source=transcript.source,
        fetched_at=transcript.fetched_at,
    )


@router.post("/{meeting_id}/analyze", response_model=ReportOut)
async def analyze_meeting(meeting_id: int, db: AsyncSession = Depends(get_db)) -> MeetingReport:
    meeting = await _get_meeting_or_404(db, meeting_id)
    return await orchestrator.run_analysis(db, meeting)


@router.get("/{meeting_id}/report", response_model=ReportOut)
async def get_report(meeting_id: int, db: AsyncSession = Depends(get_db)) -> MeetingReport:
    row = await db.execute(select(MeetingReport).where(MeetingReport.meeting_id == meeting_id))
    report = row.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="report not generated yet")
    return report


@router.post("/{meeting_id}/send-email", response_model=EmailResult)
async def send_email(meeting_id: int, db: AsyncSession = Depends(get_db)) -> EmailResult:
    meeting = await _get_meeting_or_404(db, meeting_id)
    message_id = await orchestrator.send_report_email(db, meeting)
    return EmailResult(meeting_id=meeting_id, message_id=message_id, status=meeting.status)


@router.post("/{meeting_id}/stop", response_model=StopResult)
async def stop_meeting(meeting_id: int, db: AsyncSession = Depends(get_db)) -> StopResult:
    meeting = await _get_meeting_or_404(db, meeting_id)
    provider = get_provider()
    ok = await provider.stop(meeting.native_meeting_id)
    return StopResult(meeting_id=meeting_id, stopped=ok)
