"""Async Google Calendar client.

Reads the bot account's calendar for upcoming events that carry a Google Meet
link — these are the meetings the bot was invited to. We never push; we poll
(Calendar push needs a domain-verified webhook, infeasible on ngrok for dev).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.logging_config import get_logger
from app.services.google.token import get_access_token
from app.services.http import request_with_retries

log = get_logger(__name__)

_CAL_BASE = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass
class CalendarEvent:
    event_id: str
    title: str | None
    start: datetime | None
    end: datetime | None
    meet_url: str | None
    organizer_email: str | None
    attendees: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _parse_dt(node: dict | None) -> datetime | None:
    if not node:
        return None
    value = node.get("dateTime")  # timed event
    if not value:
        return None  # all-day events have only 'date'; ignore (no Meet time)
    # Python 3.11+ fromisoformat handles the trailing 'Z' and offsets.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def _extract_meet_url(event: dict) -> str | None:
    link = event.get("hangoutLink")
    if link:
        return link
    conf = event.get("conferenceData") or {}
    for ep in conf.get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return None


class CalendarClient:
    """Read-only Calendar access for the bot's primary calendar."""

    def __init__(self, calendar_id: str = "primary") -> None:
        self._calendar_id = calendar_id

    async def list_upcoming_meet_events(
        self, *, time_min: datetime, time_max: datetime, max_results: int = 50
    ) -> list[CalendarEvent]:
        token = await get_access_token()
        params = {
            "timeMin": time_min.astimezone(timezone.utc).isoformat(),
            "timeMax": time_max.astimezone(timezone.utc).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max_results),
            "showDeleted": "false",
        }
        url = f"{_CAL_BASE}/calendars/{self._calendar_id}/events"
        resp = await request_with_retries(
            "GET", url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT
        )
        if resp.status_code != 200:
            log.error("calendar_list_failed", status=resp.status_code, body=resp.text[:300])
            resp.raise_for_status()

        items = resp.json().get("items", [])
        events: list[CalendarEvent] = []
        for item in items:
            if item.get("status") == "cancelled":
                continue
            meet_url = _extract_meet_url(item)
            if not meet_url:
                continue  # only meetings with a Meet link are actionable
            events.append(
                CalendarEvent(
                    event_id=item["id"],
                    title=item.get("summary"),
                    start=_parse_dt(item.get("start")),
                    end=_parse_dt(item.get("end")),
                    meet_url=meet_url,
                    organizer_email=(item.get("organizer") or {}).get("email"),
                    attendees=[a.get("email") for a in item.get("attendees", []) if a.get("email")],
                    raw=item,
                )
            )
        log.info("calendar_listed", total=len(items), with_meet=len(events))
        return events
