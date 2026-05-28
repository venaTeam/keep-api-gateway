"""rename camel case fields and drop enriched_fields

Revision ID: 20d5647ff365
Revises: e1932c411f61
Create Date: 2026-05-12 17:23:22.662937

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20d5647ff365"
down_revision = "e1932c411f61"
branch_labels = None
depends_on = None

# camelCase (old) -> snake_case (new)
_RENAMES = {
    "lastReceived": "last_received",
    "dismissUntil": "dismiss_until",
    "duplicateReason": "duplicate_reason",
    "firingCounter": "firing_counter",
    "firingStartTime": "firing_start_time",
    "firingStartTimeSinceLastResolved": "firing_start_time_since_last_resolved",
    "isFullDuplicate": "is_full_duplicate",
    "isPartialDuplicate": "is_partial_duplicate",
    "startedAt": "started_at",
    "unresolvedCounter": "unresolved_counter",
}


def _alert_columns():
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("alert")}


def upgrade() -> None:
    # Rename camelCase fields to snake_case and drop enriched_fields.
    # Guarded for replay-from-base: the upstream model-introspecting migration
    # (e4ad6ddc7e90) now creates snake_case columns directly, so on a fresh DB
    # there is no camelCase column to rename and these become no-ops. On a DB
    # migrated when the model still had camelCase, the renames run as before.
    existing = _alert_columns()
    with op.batch_alter_table("alert", schema=None) as batch_op:
        for old, new in _RENAMES.items():
            if old in existing and new not in existing:
                batch_op.alter_column(old, new_column_name=new)
        if "enriched_fields" in existing:
            batch_op.drop_column("enriched_fields")


def downgrade() -> None:
    # Reverse: recreate enriched_fields and rename snake_case back to camelCase.
    existing = _alert_columns()
    with op.batch_alter_table("alert", schema=None) as batch_op:
        if "enriched_fields" not in existing:
            batch_op.add_column(
                sa.Column(
                    "enriched_fields",
                    postgresql.JSONB(astext_type=sa.Text()),
                    autoincrement=False,
                    nullable=True,
                )
            )
        for old, new in _RENAMES.items():
            if new in existing and old not in existing:
                batch_op.alter_column(new, new_column_name=old)
