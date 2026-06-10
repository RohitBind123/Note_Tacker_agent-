"""FastAPI application entrypoint.

Wires logging, lifespan, and routers. Background services (calendar poller,
scheduler) are added in later phases via the lifespan hook.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes import admin, health, meetings, webhooks
from app.config import settings
from app.logging_config import configure_logging, get_logger
from app.services import runner

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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

app.include_router(health.router)
app.include_router(meetings.router)
app.include_router(admin.router)
app.include_router(webhooks.router)


@app.get("/")
async def root() -> dict:
    return {"service": "centralagent", "version": __version__, "docs": "/docs"}
