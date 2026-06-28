"""add `environment` column to the alert table

Promotes the alert environment to a first-class, queryable column. Existing rows
are backfilled to "production" via the server default, so no separate data
migration is required and ingestion never has to start sending the field.

Revision ID: alert_environment
Revises: alert_audit_covering_index
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "alert_environment"
down_revision = "alert_audit_covering_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column(
            "environment",
            sa.String(50),
            nullable=False,
            server_default="production",
        ),
    )


def downgrade() -> None:
    op.drop_column("alert", "environment")
