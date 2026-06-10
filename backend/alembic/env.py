"""Alembic environment — async, driven by app.config.

Uses the Neon DIRECT (non-pooler) connection for DDL (PgBouncer is unsuitable
for migrations). The URL and SSL come from app settings, never hardcoded.
"""
from __future__ import annotations

import asyncio
import ssl
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Make the backend package importable (env.py lives in backend/alembic/).
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db import models  # noqa: E402,F401  (import registers tables on Base.metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _connect_args() -> dict:
    return {
        "ssl": ssl.create_default_context(),
        "statement_cache_size": 0,
    }


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(
        settings.async_database_url_direct,
        connect_args=_connect_args(),
        poolclass=None,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; use a live DB connection.")
else:
    run_migrations_online()
