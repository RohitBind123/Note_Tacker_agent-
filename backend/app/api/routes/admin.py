"""Internal debug endpoints — trigger a poll / scheduler tick on demand.

Not user-facing; used during development to exercise the background flows
without waiting for the loop interval.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.logging_config import get_logger
from app.services import calendar_poller, scheduler

router = APIRouter(prefix="/admin", tags=["admin-debug"])
log = get_logger(__name__)


@router.post("/poll-calendar")
async def poll_calendar(db: AsyncSession = Depends(get_db)) -> dict:
    upserted = await calendar_poller.poll_once(db)
    return {"upserted": upserted}


@router.post("/scheduler-tick")
async def scheduler_tick() -> dict:
    await scheduler.tick()
    return {"status": "ticked"}
