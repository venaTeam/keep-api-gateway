"""add per-occurrence `received_at` column to the alert table

The alertenrichment removal relocated the per-fingerprint `last_received` to `lastalert.last_received`
(the canonical value used by the DTO/queries). This re-introduces a per-occurrence
received timestamp on the `alert` row itself, under a distinct name (`received_at`)
so it can never be confused with / override `lastalert.last_received`.

Revision ID: alert_received_at
Revises: merge_p2_alertstd
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "alert_received_at"
down_revision = "merge_p2_alertstd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column("received_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alert", "received_at")
