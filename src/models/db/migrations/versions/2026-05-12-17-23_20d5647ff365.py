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

def upgrade() -> None:
    # Rename camelCase fields to snake_case and drop enriched_fields
    with op.batch_alter_table("alert", schema=None) as batch_op:
        batch_op.alter_column("lastReceived", new_column_name="last_received")
        batch_op.alter_column("dismissUntil", new_column_name="dismiss_until")
        batch_op.alter_column("duplicateReason", new_column_name="duplicate_reason")
        batch_op.alter_column("firingCounter", new_column_name="firing_counter")
        batch_op.alter_column("firingStartTime", new_column_name="firing_start_time")
        batch_op.alter_column("firingStartTimeSinceLastResolved", new_column_name="firing_start_time_since_last_resolved")
        batch_op.alter_column("isFullDuplicate", new_column_name="is_full_duplicate")
        batch_op.alter_column("isPartialDuplicate", new_column_name="is_partial_duplicate")
        batch_op.alter_column("startedAt", new_column_name="started_at")
        batch_op.alter_column("unresolvedCounter", new_column_name="unresolved_counter")
        batch_op.drop_column("enriched_fields")

def downgrade() -> None:
    # Rename snake_case fields back to camelCase and recreate enriched_fields
    with op.batch_alter_table("alert", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "enriched_fields",
                postgresql.JSONB(astext_type=sa.Text()),
                autoincrement=False,
                nullable=True,
            )
        )
        batch_op.alter_column("last_received", new_column_name="lastReceived")
        batch_op.alter_column("dismiss_until", new_column_name="dismissUntil")
        batch_op.alter_column("duplicate_reason", new_column_name="duplicateReason")
        batch_op.alter_column("firing_counter", new_column_name="firingCounter")
        batch_op.alter_column("firing_start_time", new_column_name="firingStartTime")
        batch_op.alter_column("firing_start_time_since_last_resolved", new_column_name="firingStartTimeSinceLastResolved")
        batch_op.alter_column("is_full_duplicate", new_column_name="isFullDuplicate")
        batch_op.alter_column("is_partial_duplicate", new_column_name="isPartialDuplicate")
        batch_op.alter_column("started_at", new_column_name="startedAt")
        batch_op.alter_column("unresolved_counter", new_column_name="unresolvedCounter")
