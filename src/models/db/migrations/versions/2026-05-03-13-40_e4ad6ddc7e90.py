"""add_alert_columns

Revision ID: e4ad6ddc7e90
Revises: 9dd1be4539e0
Create Date: 2026-05-03 13:40:26.861963

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "e4ad6ddc7e90"
down_revision = "9dd1be4539e0"
branch_labels = None
depends_on = None


from src.models.db.alert import Alert

# Infrastructure columns that existed before this migration
_INFRA_COLUMNS = {
    "id", "tenant_id", "timestamp", "provider_type", "provider_id",
    "fingerprint", "alert_hash"
}

def upgrade() -> None:
    for col in Alert.__table__.columns:
        if col.name not in _INFRA_COLUMNS and col.name != "event":
            col_copy = col.copy()
            # Temporarily make all new columns nullable to prevent NotNullViolation on existing rows
            col_copy.nullable = True
            op.add_column("alert", col_copy)


def downgrade() -> None:
    for col in Alert.__table__.columns:
        if col.name not in _INFRA_COLUMNS and col.name != "event":
            op.drop_column("alert", col.name)
