"""FastAPI application entrypoint.

Wires logging, lifespan, and routers. Background services (calendar poller,
scheduler) are added in later phases via the lifespan hook.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request

from app import __version__
from app.api.routes import admin, health, meetings, webhooks
from app.config import settings
from app.logging_config import configure_logging, get_logger
from app.services import runner

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = settings.missing_required()
    if missing:
        if settings.is_production:
            raise RuntimeError(f"missing required config in production: {missing}")
        log.warning("config_incomplete", missing=missing)

    log.info(
        "app_startup",
        env=settings.app_env,
        version=__version__,
        gemini_model=settings.gemini_model,
        vexa_base=settings.vexa_api_base,
        bot_email=settings.bot_google_email,
    )
    runner.start()
    yield
    await runner.stop()
    log.info("app_shutdown")


app = FastAPI(
    title="CentralAgent — Meeting Intelligence",
    version=__version__,
    lifespan=lifespan,
)

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Bind a request id to every log line in the request, and echo it back."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["X-Request-ID"] = request_id
    return response


app.include_router(health.router)
app.include_router(meetings.router)
app.include_router(admin.router)
app.include_router(webhooks.router)


@app.get("/")
async def root() -> dict:
    return {"service": "centralagent", "version": __version__, "docs": "/docs"}
