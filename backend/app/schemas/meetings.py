"""Schemas for meeting endpoints."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import MeetingStatus


class DispatchRequest(BaseModel):
    """Manually send a bot to a Meet URL (P2 path)."""

    meet_url: str = Field(..., description="Full Google Meet URL or the bare abc-defg-hij code")
    title: str | None = None
    organizer_email: str | None = None
    bot_name: str = "CentralAgent Notetaker"


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    google_event_id: str | None = None
    platform: str
    native_meeting_id: str
    meet_url: str
    title: str | None = None
    organizer_email: str | None = None
    status: MeetingStatus
    vexa_bot_id: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TranscriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    meeting_id: int
    segment_count: int
    full_text: str
    source: str
    fetched_at: datetime | None = None


class StopResult(BaseModel):
    meeting_id: int
    stopped: bool


class EmailResult(BaseModel):
    meeting_id: int
    message_id: str
    status: MeetingStatus


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    meeting_id: int
    summary: str | None = None
    decisions: list | None = None
    action_items: list | None = None
    risks: list | None = None
    next_steps: list | None = None
    model_used: str | None = None
    created_at: datetime
