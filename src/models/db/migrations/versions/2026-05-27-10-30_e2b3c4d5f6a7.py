"""phase2 P2-M2: drop misplaced columns from alert

Revision ID: e2b3c4d5f6a7
Revises: d1a2b3c4e5f6
Create Date: 2026-05-27 10:30:00.000000

Phase 2 / task P2-M2 of the alertenrichment removal (PHASE2_IMPLEMENTATION_PLAN.md §2).

Drops the mutable cross-occurrence state that Migration 1 incorrectly placed on the
append-only ``alert`` table. This state now lives as typed columns on ``lastalert``
(added by Phase 1 c1d2e3f4a5b6) and is read/written there by all three services.

Columns dropped from ``alert``:
  last_received, firing_counter, unresolved_counter, started_at, firing_start_time,
  firing_start_time_since_last_resolved (system tracking -> lastalert.*);
  assignee, dismissed, dismiss_until, note (user state -> lastalert.*);
  incident (denormalized from the AlertToIncident / LastAlertToIncident join — always
  derived via join now).

KEEP on ``alert``: status, severity, description (immutable provider values).

NOTE — ``enriched_fields`` and ``extra_data`` were already dropped by Migration 1, so
they are intentionally NOT listed here.

WARNING — RUN LAST. This is the only irreversible-in-place step of Phase 2. It MUST run only
after ALL THREE services (keep-event-handler, keep-api-gateway, keep-workflows) have
deployed the Phase 2 code that no longer maps these columns on the ``Alert`` ORM model.
The ``Alert`` SQLModel SELECTs every mapped column; dropping a column the ORM still maps
breaks every alert query. Order: remove from ORM (code deploy) first, drop in DB last
(this migration). See PHASE2_IMPLEMENTATION_PLAN.md §5 (the column-drop gate).

The ``alertenrichment`` table itself is untouched here — it is dropped in Phase 3.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "e2b3c4d5f6a7"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None

# Columns to drop from ``alert``, paired with their pre-Phase-2 (re-add) definition for
# the downgrade. (name, type, nullable, server_default)
_DROPPED_COLUMNS = [
    ("last_received", postgresql.TIMESTAMP(timezone=True), True, None),
    ("firing_counter", sa.Integer(), False, sa.text("0")),
    ("unresolved_counter", sa.Integer(), False, sa.text("0")),
    ("started_at", sa.String(255), True, None),
    ("firing_start_time", sa.String(255), True, None),
    ("firing_start_time_since_last_resolved", sa.String(255), True, None),
    ("assignee", sa.String(255), True, None),
    ("dismissed", sa.Boolean(), False, sa.text("false")),
    ("dismiss_until", sa.String(255), True, None),
    ("note", sa.Text(), True, None),
    ("incident", sa.String(255), True, None),
]


def upgrade() -> None:
    for name, *_ in _DROPPED_COLUMNS:
        op.drop_column("alert", name)


def downgrade() -> None:
    # Re-add with the pre-Phase-2 types/defaults. Data is NOT restored — the values now
    # live on ``lastalert``; a rollback would leave these columns at their defaults/NULL.
    for name, type_, nullable, server_default in reversed(_DROPPED_COLUMNS):
        op.add_column(
            "alert",
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )
