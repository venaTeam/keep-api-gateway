"""backfill_alert_data

Revision ID: 5dfe012ad560
Revises: e4ad6ddc7e90
Create Date: 2026-05-03 13:41:27.050743

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "5dfe012ad560"
down_revision = "e4ad6ddc7e90"
branch_labels = None
depends_on = None


from alembic import op
from sqlalchemy import text
from src.models.db.alert import Alert

_INFRA_COLUMNS = {
    "id", "tenant_id", "timestamp", "provider_type", "provider_id",
    "fingerprint", "alert_hash"
}

def _get_payload_columns():
    return [col.name for col in Alert.__table__.columns
            if col.name not in _INFRA_COLUMNS and col.name not in ("event", "extra_data")]

BATCH_SIZE = 10_000

def upgrade() -> None:
    conn = op.get_bind()
    payload_cols = _get_payload_columns()
    
    set_clauses_list = []
    for col_name in payload_cols:
        col = Alert.__table__.columns[col_name]
        # PostgreSQL JSON ->> returns text. We must cast to boolean/integer.
        if isinstance(col.type, sa.Boolean):
            set_clauses_list.append(f'"{col_name}" = (event->>\'{col_name}\')::boolean')
        elif isinstance(col.type, sa.Integer):
            set_clauses_list.append(f'"{col_name}" = (event->>\'{col_name}\')::integer')
        elif isinstance(col.type, sa.JSON) or col_name in ("enriched_fields", "labels", "incident_dto", "assignees", "source"):
            # If the field is a dictionary/list, extract directly as JSON instead of text
            set_clauses_list.append(f'"{col_name}" = event->\'{col_name}\'')
        else:
            set_clauses_list.append(f'"{col_name}" = event->>\'{col_name}\'')

    set_clauses = ", ".join(set_clauses_list)
    remove_keys = ", ".join(payload_cols)

    while True:
        result = conn.execute(text(f"""
            WITH batch AS (
                SELECT id FROM alert
                WHERE event IS NOT NULL
                  AND "{payload_cols[0]}" IS NULL
                LIMIT {BATCH_SIZE}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE alert SET
                {set_clauses},
                "extra_data" = event - '{{{remove_keys}}}'::text[]
            FROM batch
            WHERE alert.id = batch.id
        """))

        if result.rowcount == 0:
            break

        conn.commit()


def downgrade() -> None:
    pass
