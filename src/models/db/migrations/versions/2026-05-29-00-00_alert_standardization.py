"""alert standardization

Revision ID: alert_standardization
Revises: a1b2c3d4e5f6
Create Date: 2026-05-29 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "alert_standardization"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert", sa.Column("site", sa.String(length=100), nullable=True))
    op.add_column("alert", sa.Column("impact", sa.TEXT(), nullable=True))
    op.add_column("alert", sa.Column("runbook_url", sa.TEXT(), nullable=True))
    op.add_column("alert", sa.Column("alert_rule_url", sa.TEXT(), nullable=True))
    op.drop_column("alert", "message")


def downgrade() -> None:
    op.add_column("alert", sa.Column("message", sa.String(length=800), nullable=True))
    op.drop_column("alert", "alert_rule_url")
    op.drop_column("alert", "runbook_url")
    op.drop_column("alert", "impact")
    op.drop_column("alert", "site")
