"""add meetings.gmail_message_id for Gmail-invite-sourced meetings

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-11

Adds a nullable ``gmail_message_id`` column (the Gmail message-id used as an
idempotency key for meetings detected via the Gmail invite scanner, as opposed
to the calendar-poller path which keys on ``google_event_id``).

Uniqueness is enforced via a PARTIAL unique index
  (WHERE gmail_message_id IS NOT NULL)
so the many NULL rows from Calendar-sourced meetings never violate it.

Pre-migration audit: every existing row has gmail_message_id = NULL (brand-new
column), so zero rows violate the partial unique index — safe to add directly.

The ADD COLUMN is non-blocking on Postgres 11+ (catalog-only change for a
nullable column with no default).
"""
from __future__ import annotations

from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS gmail_message_id VARCHAR(256)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_meetings_gmail_message_id "
        "ON meetings (gmail_message_id) "
        "WHERE gmail_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_meetings_gmail_message_id")
    op.execute("ALTER TABLE meetings DROP COLUMN IF EXISTS gmail_message_id")
