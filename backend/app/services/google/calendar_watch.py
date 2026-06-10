"""Google Calendar push-notification channel management (events.watch).

Registers a webhook channel so Google pushes a ping whenever the bot's calendar
changes. REQUIRES a verified-domain HTTPS receiver — so it is gated behind
``CALENDAR_PUSH_ENABLED`` and only used in production. In dev it stays off and
the poller drives detection.

Channels expire (max ~7 days/ a few hours depending on resource); the runner
renews before expiry.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.services.google.token import get_access_token

log = get_logger(__name__)

_CAL_BASE = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass
class WatchChannel:
    channel_id: str
    resource_id: str
    expiration_ms: int | None


def _webhook_address() -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/webhooks/google/calendar"


async def register_watch(calendar_id: str = "primary") -> WatchChannel:
    """Create a push channel for the bot's calendar. Caller must ensure
    push is enabled and the public URL is a verified HTTPS domain."""
    token = await get_access_token()
    channel_id = str(uuid.uuid4())
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": _webhook_address(),
        "token": settings.calendar_webhook_token,
    }
    url = f"{_CAL_BASE}/calendars/{calendar_id}/events/watch"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code not in (200, 201):
        log.error("calendar_watch_register_failed", status=resp.status_code, body=resp.text[:300])
        resp.raise_for_status()
    data = resp.json()
    channel = WatchChannel(
        channel_id=data.get("id", channel_id),
        resource_id=data.get("resourceId", ""),
        expiration_ms=int(data["expiration"]) if data.get("expiration") else None,
    )
    log.info(
        "calendar_watch_registered",
        channel_id=channel.channel_id,
        expiration_ms=channel.expiration_ms,
    )
    return channel


async def stop_watch(channel: WatchChannel) -> None:
    token = await get_access_token()
    body = {"id": channel.channel_id, "resourceId": channel.resource_id}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_CAL_BASE}/channels/stop", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    log.info("calendar_watch_stopped", channel_id=channel.channel_id, status=resp.status_code)
