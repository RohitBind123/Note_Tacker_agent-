"""Inbound webhooks.

Google Calendar push receiver: Google sends a header-only POST when the bot's
calendar changes (no body). We validate the channel token, then trigger a
calendar sync (the same poll_once used by the background loop). Fast 200 always
(Google retries on non-2xx, and we must not block its delivery).
"""
from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.db.session import async_session_factory
from app.logging_config import get_logger
from app.services import calendar_poller

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)


@router.post("/google/calendar")
async def google_calendar_push(
    request: Request,
    x_goog_channel_token: str | None = Header(default=None),
    x_goog_resource_state: str | None = Header(default=None),
    x_goog_channel_id: str | None = Header(default=None),
) -> PlainTextResponse:
    # Validate the shared token to reject spoofed calls.
    if settings.calendar_webhook_token and x_goog_channel_token != settings.calendar_webhook_token:
        log.warning("calendar_webhook_bad_token", channel_id=x_goog_channel_id)
        return PlainTextResponse("ok", status_code=200)  # don't reveal; don't let Google retry-storm

    log.info(
        "calendar_webhook_received",
        resource_state=x_goog_resource_state,
        channel_id=x_goog_channel_id,
    )

    # "sync" is the initial handshake (no change yet); real changes are "exists".
    if x_goog_resource_state and x_goog_resource_state != "sync":
        try:
            async with async_session_factory() as db:
                upserted = await calendar_poller.poll_once(db)
            log.info("calendar_webhook_synced", upserted=upserted)
        except Exception:
            log.exception("calendar_webhook_sync_error")

    return PlainTextResponse("ok", status_code=200)
