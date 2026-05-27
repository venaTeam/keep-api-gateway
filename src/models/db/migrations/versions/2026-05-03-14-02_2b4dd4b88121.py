"""drop_event_column

Revision ID: 2b4dd4b88121
Revises: 5dfe012ad560
Create Date: 2026-05-03 14:02:01.428644

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "2b4dd4b88121"
down_revision = "5dfe012ad560"
branch_labels = None
depends_on = None


from src.models.db.alert import Alert
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

_INFRA_COLUMNS = {
    "id", "tenant_id", "timestamp", "provider_type", "provider_id",
    "fingerprint", "alert_hash"
}

def _get_payload_columns():
    return [col.name for col in Alert.__table__.columns
            if col.name not in _INFRA_COLUMNS and col.name not in ("event", "extra_data")]

def upgrade() -> None:
    op.drop_column("alert", "event")


def downgrade() -> None:
    op.add_column("alert", sa.Column("event", PG_JSONB, nullable=True))

    payload_cols = _get_payload_columns()
    json_build = ", ".join(f"'{col}', {col}" for col in payload_cols)
    op.execute(f"""
        UPDATE alert SET event = jsonb_build_object({json_build})
    """)
