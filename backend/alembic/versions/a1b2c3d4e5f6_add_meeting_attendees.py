"""add meetings.attendees (JSONB) for configurable email recipients

Adds a nullable JSONB column holding the invited guest emails, so the insight
email can be sent to all attendees (not just the organizer) when
EMAIL_RECIPIENTS=all_attendees. Nullable add -> non-blocking (catalog-only on
Postgres 11+). Re-runnable via IF NOT EXISTS.

Revision ID: a1b2c3d4e5f6
Revises: baf0f4995237
"""
from __future__ import annotations

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "baf0f4995237"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE meetings ADD COLUMN IF NOT EXISTS attendees JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE meetings DROP COLUMN IF EXISTS attendees")
