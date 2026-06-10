"""Async engine + session factory for the running app.

Targets the Neon POOLED endpoint. Two Neon/asyncpg specifics handled here:
  1. SSL is required -> pass an SSLContext via connect_args (asyncpg ignores the
     libpq ``sslmode`` query param, which we strip in config).
  2. The pooled endpoint is PgBouncer (transaction mode), which is incompatible
     with server-side prepared statements -> disable both asyncpg's statement
     cache and SQLAlchemy's prepared-statement cache.

Pool is intentionally small: the Neon pooler does the fan-out, so the app's own
pool stays lean (see production-performance rules).
"""
from __future__ import annotations

import ssl
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _ssl_context() -> ssl.SSLContext:
    """Default verifying SSL context (Neon presents a valid public cert)."""
    return ssl.create_default_context()


def _app_url() -> str:
    # Disable SQLAlchemy's prepared-statement cache for PgBouncer compatibility.
    base = settings.async_database_url
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}prepared_statement_cache_size=0"


engine = create_async_engine(
    _app_url(),
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=300,
    pool_timeout=30,
    connect_args={
        "ssl": _ssl_context(),
        "statement_cache_size": 0,  # asyncpg-level, required for PgBouncer
        "server_settings": {"application_name": "centralagent"},
    },
)

# Read-mostly; objects stay usable after commit (we serialize before returning).
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            log.exception("db_session_error")
            raise


async def ping() -> bool:
    """Lightweight connectivity check used by /healthz/db."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
