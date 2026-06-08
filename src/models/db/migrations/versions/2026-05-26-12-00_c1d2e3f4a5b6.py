"""phase1: add enrichment + tracking columns to lastalert

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-05-26 12:00:00.000000

Phase 1 / task 1.1 of the alertenrichment removal (ALERTENRICHMENT_ROLLOUT_PLAN.md).

Adds typed columns to ``lastalert``:
  - user enrichment state (later sourced from alertenrichment):
      status, status_disposable, dismiss_mode, dismissed_until, assignee, note
  - system tracking fields (later sourced from alert):
      last_received, firing_counter, unresolved_counter,
      started_at, firing_start_time, firing_start_time_since_last_resolved

Does NOT drop alertenrichment and does NOT change any code path (NF6).

IMPORTANT — Phase 1 runs while the OLD code is still live. The old
``set_last_alert()`` inserts a LastAlert WITHOUT these columns, so every NOT NULL
column MUST keep a ``server_default`` or those inserts would raise
NotNullViolation. The defaults are left in place through Phase 1; a later migration
may drop them once the code writes the columns explicitly.

Types match Migration 1's final alert schema:
  - last_received   -> TIMESTAMPTZ  (alert.last_received is timestamptz since a1b2c3d4e5f6)
  - dismissed_until -> TIMESTAMPTZ  (expiry timestamp; range-queried by dismissal_expiry_bl)
  - started_at / firing_start_time / firing_start_time_since_last_resolved
                    -> String(255)  (still String(255) on alert)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c1d2e3f4a5b6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

# (name, type, nullable, server_default)
_NEW_COLUMNS = [
    ("status", sa.String(50), True, None),
    ("status_disposable", sa.Boolean(), False, sa.text("false")),
    ("dismiss_mode", sa.String(20), True, None),
    ("dismissed_until", postgresql.TIMESTAMP(timezone=True), True, None),
    ("assignee", sa.String(255), True, None),
    ("note", sa.Text(), True, None),
    ("last_received", postgresql.TIMESTAMP(timezone=True), True, None),
    ("firing_counter", sa.Integer(), False, sa.text("0")),
    ("unresolved_counter", sa.Integer(), False, sa.text("0")),
    ("started_at", sa.String(255), True, None),
    ("firing_start_time", sa.String(255), True, None),
    ("firing_start_time_since_last_resolved", sa.String(255), True, None),
]


def upgrade() -> None:
    for name, type_, nullable, server_default in _NEW_COLUMNS:
        op.add_column(
            "lastalert",
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )


def downgrade() -> None:
    for name, *_ in reversed(_NEW_COLUMNS):
        op.drop_column("lastalert", name)
