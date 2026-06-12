"""enforce one in-flight meeting row per Meet room code

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-12

The true business identity of a meeting is its Google Meet room code
(``native_meeting_id``, e.g. ``abc-defg-hij``) — a live Meet room cannot host two
concurrent bot sessions. Until now uniqueness was only enforced on the *detection
source* ids (``google_event_id`` UNIQUE, ``gmail_message_id`` partial UNIQUE), so
two rows for one room could be created (two calendar events sharing a recurring
Meet link; a gmail invite detected before the calendar poll). Each extra row
dispatches another bot -> duplicate transcript / analysis / insight email.

This adds a PARTIAL UNIQUE INDEX so at most ONE non-terminal (in-flight) row can
exist per Meet code. A second insert for an already-in-flight code fails with
IntegrityError, which the app skips gracefully (see app/services/meeting_dedup.py
and the calendar-poller / Gmail-scanner cross-source guards). Terminal rows
(COMPLETED / FAILED_* / CANCELLED) are excluded, so a genuinely NEW meeting that
reuses an old code is unaffected.

Predicate casing: the ``status`` column is a non-native enum
(``native_enum=False``) that persists the enum MEMBER NAMES, so stored values are
UPPERCASE (``SCHEDULED``, ``ACTIVE``, ...). The predicate wraps the column in
``upper(status)`` — ``upper`` is IMMUTABLE so it is valid in a partial-index
predicate, and it makes the index correct regardless of any future casing drift
(the index is a pure backstop, never an ON CONFLICT inference target, so a
functional predicate is fine).

Pre-migration audit (run read-only against prod 2026-06-12 before writing this):
  - SELECT status, count(*) ... -> only COMPLETED (17) + FAILED_JOIN (4); statuses
    confirmed stored UPPERCASE.
  - native_meeting_id with >1 non-terminal row -> 0 rows.
  - total in-flight rows -> 0.
  Zero rows violate the new index, so it is safe to create directly with no dedup
  step. ``IF NOT EXISTS`` keeps it re-runnable after a partial failure.
"""
from __future__ import annotations

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_INFLIGHT_STATUSES = "'PENDING','SCHEDULED','JOINING','ACTIVE','PROCESSING'"


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_meetings_active_native "
        "ON meetings (native_meeting_id) "
        f"WHERE upper(status) IN ({_INFLIGHT_STATUSES})"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_meetings_active_native")
