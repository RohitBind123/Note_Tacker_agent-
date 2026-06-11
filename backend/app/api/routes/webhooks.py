"""Inbound webhooks.

Two receivers:

  POST /webhooks/google/calendar
    Google Calendar push: a header-only POST when the bot's calendar changes
    (no body). We validate the channel token, then trigger a calendar sync.

  POST /webhooks/vexa
    Vexa delivery (HMAC-signed). On ``meeting.completed`` we finalize the
    meeting *instantly* — transcript -> insights -> email — instead of waiting
    for the next scheduler tick, which is the Phase-1 insight-latency fix. The
    handler is fully idempotent (dedup on event_id; finalize is lock-guarded and
    a COMPLETED meeting is a no-op) and returns a fast 200 so Vexa never blocks
    on our pipeline.

Both return a fast 2xx: the providers retry on non-2xx, so the only non-2xx we
ever emit is a 401 for a Vexa delivery that fails signature/replay verification
(a spoofed or stale call we deliberately reject).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from app.config import settings
from app.db.models import Meeting, MeetingStatus
from app.db.session import async_session_factory
from app.logging_config import get_logger
from app.services import calendar_poller, orchestrator
from app.services.copilot import webhook as vexa_webhook

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


# --- Vexa webhook (meeting.completed -> instant finalize) ---------------------

# In-process dedup of event_ids. The whole app is one process, so a bounded ring
# is enough to absorb Vexa's at-least-once delivery / retries. finalize_meeting
# is itself idempotent, so this is an optimization (skip redundant work), not the
# only line of defense.
_SEEN_EVENT_IDS: deque[str] = deque(maxlen=2048)
_seen_event_set: set[str] = set()

# Keep references to in-flight finalize tasks so they aren't garbage-collected
# mid-run (asyncio holds only weak refs to bare tasks).
_finalize_tasks: set[asyncio.Task] = set()


def _already_seen(event_id: str) -> bool:
    """Record + report whether this event_id was already handled (idempotency)."""
    if not event_id:
        return False
    if event_id in _seen_event_set:
        return True
    if len(_SEEN_EVENT_IDS) == _SEEN_EVENT_IDS.maxlen:
        oldest = _SEEN_EVENT_IDS.popleft()
        _seen_event_set.discard(oldest)
    _SEEN_EVENT_IDS.append(event_id)
    _seen_event_set.add(event_id)
    return False


async def _finalize_meeting_now(meeting_id: int) -> None:
    """Run the insight pipeline for an ended meeting in its own session.

    Lock-guarded inside orchestrator.finalize_meeting, so this can race the
    scheduler's process_pending pass without sending two emails.
    """
    try:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None:
                return
            await orchestrator.finalize_meeting(db, meeting)
    except Exception:
        log.exception("vexa_webhook_finalize_error", meeting_id=meeting_id)


def _spawn_finalize(meeting_id: int) -> None:
    task = asyncio.create_task(_finalize_meeting_now(meeting_id))
    _finalize_tasks.add(task)
    task.add_done_callback(_finalize_tasks.discard)


async def _find_meeting_id(platform: str | None, native_id: str) -> int | None:
    """Most-recent meeting row matching the webhook's (platform, native_id)."""
    async with async_session_factory() as db:
        stmt = select(Meeting.id).where(Meeting.native_meeting_id == native_id)
        if platform:
            stmt = stmt.where(Meeting.platform == platform)
        stmt = stmt.order_by(Meeting.id.desc()).limit(1)
        return (await db.execute(stmt)).scalar_one_or_none()


async def _mark_processing(meeting_id: int) -> None:
    """Move a still-live meeting into PROCESSING (durable handoff to finalize).

    Setting PROCESSING synchronously means the scheduler's process_pending pass
    is a safety net even if the spawned finalize task dies — the meeting can't be
    lost. finalize_meeting (and its lock) make the two paths converge to one
    email.
    """
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, meeting_id)
        if meeting is None:
            return
        if meeting.status in (MeetingStatus.JOINING, MeetingStatus.ACTIVE):
            meeting.status = MeetingStatus.PROCESSING
            meeting.end_time = meeting.end_time or datetime.now(timezone.utc)
            await db.commit()
            log.info("vexa_webhook_marked_processing", meeting_id=meeting_id)


@router.post("/vexa")
async def vexa_webhook_receiver(
    request: Request,
    x_webhook_signature: str | None = Header(default=None),
    x_webhook_timestamp: str | None = Header(default=None),
) -> JSONResponse:
    raw_body = await request.body()

    # 1. Authenticity + replay protection. Fail closed: no secret configured ->
    #    reject everything (a misconfig must not silently accept spoofed calls).
    if not vexa_webhook.verify_signature(
        settings.vexa_webhook_secret, x_webhook_timestamp, raw_body, x_webhook_signature
    ):
        log.warning("vexa_webhook_bad_signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    if not vexa_webhook.is_fresh_timestamp(x_webhook_timestamp, time.time()):
        log.warning("vexa_webhook_stale_timestamp", timestamp=x_webhook_timestamp)
        return JSONResponse({"error": "stale timestamp"}, status_code=401)

    # 2. Parse. A malformed/unknown payload is acknowledged (200) so Vexa stops
    #    retrying — there's nothing for us to act on.
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError):
        log.warning("vexa_webhook_bad_json")
        return JSONResponse({"status": "ignored"}, status_code=200)

    event = vexa_webhook.parse_webhook_event(body)
    if event is None:
        return JSONResponse({"status": "ignored"}, status_code=200)

    # 3. Idempotency: a replayed/duplicate event_id is acknowledged but not re-run.
    if _already_seen(event.event_id):
        log.info("vexa_webhook_duplicate", event_id=event.event_id, event_type=event.event_type)
        return JSONResponse({"status": "duplicate"}, status_code=200)

    log.info(
        "vexa_webhook_received",
        event_id=event.event_id,
        event_type=event.event_type,
        native_meeting_id=event.native_meeting_id,
    )

    # 4. Act. Only meeting.completed drives the instant-finalize path today.
    if event.is_meeting_completed and event.native_meeting_id:
        meeting_id = await _find_meeting_id(event.platform, event.native_meeting_id)
        if meeting_id is None:
            log.warning(
                "vexa_webhook_meeting_not_found",
                native_meeting_id=event.native_meeting_id,
                platform=event.platform,
            )
        else:
            await _mark_processing(meeting_id)
            _spawn_finalize(meeting_id)  # fire-and-forget; 200 returns immediately

    return JSONResponse({"status": "ok"}, status_code=200)
