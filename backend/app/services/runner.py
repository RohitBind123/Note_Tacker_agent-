"""Background runner — drives the calendar poller and the scheduler loops.

Started/stopped by the FastAPI lifespan. Each loop is resilient: an error in one
tick is logged and the loop continues (a transient Google/Vexa/DB hiccup must not
kill the background worker).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.config import settings
from app.db.session import async_session_factory
from app.logging_config import get_logger
from app.services import calendar_poller, scheduler
from app.services.google import calendar_watch

log = get_logger(__name__)

_tasks: list[asyncio.Task] = []
_stop = asyncio.Event()
_watch_channel: calendar_watch.WatchChannel | None = None


async def _maybe_register_calendar_push() -> None:
    """Register a Calendar push channel in production (verified-domain HTTPS).
    No-op in dev: push needs a verified domain that ngrok can't satisfy."""
    global _watch_channel
    if not settings.calendar_push_enabled:
        log.info("calendar_push_disabled", reason="CALENDAR_PUSH_ENABLED=false; poller is primary")
        return
    if not settings.public_base_url.startswith("https://"):
        log.warning("calendar_push_skipped", reason="PUBLIC_BASE_URL is not https")
        return
    try:
        _watch_channel = await calendar_watch.register_watch()
    except Exception:
        log.exception("calendar_push_register_failed")


async def _poll_calendar_once() -> None:
    async with async_session_factory() as db:
        await calendar_poller.poll_once(db)


async def _loop(name: str, interval: int, fn: Callable[[], Awaitable[None]]) -> None:
    log.info("loop_started", loop=name, interval_seconds=interval)
    # Small initial delay so startup logs settle before the first tick.
    try:
        await asyncio.wait_for(_stop.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    while not _stop.is_set():
        try:
            await fn()
        except Exception:
            log.exception("loop_tick_error", loop=name)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("loop_stopped", loop=name)


def start() -> None:
    """Launch the background loops."""
    _stop.clear()
    _tasks.append(
        asyncio.create_task(
            _loop("calendar_poller", settings.calendar_poll_interval_seconds, _poll_calendar_once)
        )
    )
    _tasks.append(
        asyncio.create_task(
            _loop("scheduler", settings.scheduler_interval_seconds, scheduler.tick)
        )
    )
    # Calendar push registration (prod only); poller above is the always-on fallback.
    _tasks.append(asyncio.create_task(_maybe_register_calendar_push()))
    log.info("background_runner_started", loops=2, push_enabled=settings.calendar_push_enabled)


async def stop() -> None:
    """Signal loops to stop and await them."""
    _stop.set()
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    if _watch_channel is not None:
        try:
            await calendar_watch.stop_watch(_watch_channel)
        except Exception:
            log.exception("calendar_push_stop_failed")
    log.info("background_runner_stopped")
