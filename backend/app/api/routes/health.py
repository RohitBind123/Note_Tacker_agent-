"""Health/liveness/readiness endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.db.session import ping
from app.logging_config import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/health")
async def health() -> dict:
    """Liveness — process is up. No external dependencies touched."""
    return {"status": "ok", "service": "centralagent", "version": __version__, "env": settings.app_env}


@router.get("/healthz/db")
async def health_db() -> dict:
    """Readiness — verifies the database is reachable."""
    try:
        await ping()
        log.info("healthz_db_ok")
        return {"status": "ok", "database": "reachable"}
    except Exception as exc:  # surfaced as 200 with detail so probes see the reason
        log.error("healthz_db_failed", error=str(exc))
        return {"status": "error", "database": "unreachable", "detail": str(exc)}
