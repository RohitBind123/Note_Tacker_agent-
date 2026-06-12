"""add meetings.email_attempts for bounded insight-email retry

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-12

A transient SMTP failure used to strand a meeting in EMAIL_FAILED forever:
``process_pending`` only re-picked rows in PROCESSING, so the insight email was
never retried (under-delivery — the inverse of the duplicate-email problem). This
adds a counter so the scheduler can retry EMAIL_FAILED meetings a bounded number
of times (settings.email_max_attempts) and then stop, instead of looping forever
on a permanently-broken recipient.

Nullable with a server default of 0 (treated as 0 in code) — a constant default
is a catalog-only, non-blocking change on Postgres 11+, and keeping the column
nullable follows the migration-safety rule (no NOT NULL on an ADD COLUMN against
existing rows). ``IF NOT EXISTS`` keeps it re-runnable.
"""
from __future__ import annotations

from alembic import op


revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE meetings "
        "ADD COLUMN IF NOT EXISTS email_attempts INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE meetings DROP COLUMN IF EXISTS email_attempts")
