"""convert alert.last_received to timestamptz

Revision ID: a1b2c3d4e5f6
Revises: 20d5647ff365
Create Date: 2026-05-18 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "20d5647ff365"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "alert",
        "last_received",
        existing_type=sa.String(length=255),
        type_=postgresql.TIMESTAMP(timezone=True),
        existing_nullable=True,
        postgresql_using="last_received::timestamptz",
    )


def downgrade() -> None:
    op.alter_column(
        "alert",
        "last_received",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        type_=sa.String(length=255),
        existing_nullable=True,
        postgresql_using="last_received::text",
    )
