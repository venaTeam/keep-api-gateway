"""backfill_alert_data

Revision ID: 5dfe012ad560
Revises: e4ad6ddc7e90
Create Date: 2026-05-03 13:41:27.050743

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "5dfe012ad560"
down_revision = "e4ad6ddc7e90"
branch_labels = None
depends_on = None


# The payload column set + types are FROZEN to the Alert model as of the
# revision that introduced this migration (commit 589070e, 2026-05-06), rather
# than introspected from the live model. Introspecting the live model made this
# backfill non-deterministic: as the model evolved (camelCase -> snake_case,
# columns moved onto LastAlert), the generated SQL referenced columns that don't
# exist at this point in the chain — which Postgres rejects even on an empty
# table — breaking replay-from-base. The types here only select the
# JSON-extraction cast; the names match the columns e4ad6ddc7e90 creates. On a
# fresh DB the table is empty, so this is effectively a no-op beyond parsing.

_INFRA_COLUMNS = {
    "id", "tenant_id", "timestamp", "provider_type", "provider_id",
    "fingerprint", "alert_hash"
}

# (name, type) frozen from the 2026-05-06 Alert model, excluding infra columns,
# ``event`` (the source JSON) and ``extra_data`` (the overflow column).
_PAYLOAD_COLUMNS = [
    ("application", sa.String(200)),
    ("object", sa.String(200)),
    ("node_name", sa.String(200)),
    ("severity", sa.String(50)),
    ("message", sa.String(800)),
    ("operator", sa.String(100)),
    ("time_created", sa.String(50)),
    ("network", sa.String(50)),
    ("timezone", sa.String(50)),
    ("custom_key", sa.String(255)),
    ("expiry_in_minutes", sa.Integer()),
    ("source", sa.String(255)),
    ("service", sa.String(255)),
    ("key_field", sa.String(255)),
    ("name", sa.String(255)),
    ("status", sa.String(50)),
    ("description", sa.Text()),
    ("lastReceived", sa.String(255)),
    ("isFullDuplicate", sa.Boolean()),
    ("isPartialDuplicate", sa.Boolean()),
    ("duplicateReason", sa.String(255)),
    ("note", sa.Text()),
    ("assignee", sa.String(255)),
    ("incident", sa.String(255)),
    ("dismissUntil", sa.String(255)),
    ("dismissed", sa.Boolean()),
    ("enriched_fields", sa.JSON()),
    ("startedAt", sa.String(255)),
    ("firingCounter", sa.Integer()),
    ("unresolvedCounter", sa.Integer()),
    ("firingStartTime", sa.String(255)),
    ("firingStartTimeSinceLastResolved", sa.String(255)),
]

BATCH_SIZE = 10_000

def upgrade() -> None:
    conn = op.get_bind()
    # `extra_data` is a transient overflow column (dropped later by e1932).
    # e4ad6ddc7e90 creates it, but keep this idempotent guard for DBs patched
    # out-of-band before extracting unknown event keys into it.
    op.execute("ALTER TABLE alert ADD COLUMN IF NOT EXISTS extra_data jsonb")
    payload_cols = [name for name, _type in _PAYLOAD_COLUMNS]

    set_clauses_list = []
    for col_name, col_type in _PAYLOAD_COLUMNS:
        # PostgreSQL JSON ->> returns text. We must cast to boolean/integer.
        if isinstance(col_type, sa.Boolean):
            set_clauses_list.append(f'"{col_name}" = (event->>\'{col_name}\')::boolean')
        elif isinstance(col_type, sa.Integer):
            set_clauses_list.append(f'"{col_name}" = (event->>\'{col_name}\')::integer')
        elif isinstance(col_type, sa.DateTime):
            # frozen model stores these as text; an explicit cast would be needed otherwise.
            set_clauses_list.append(f'"{col_name}" = (event->>\'{col_name}\')::timestamptz')
        elif isinstance(col_type, sa.JSON) or col_name in ("enriched_fields", "labels", "incident_dto", "assignees", "source"):
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
                  AND extra_data IS NULL
                LIMIT {BATCH_SIZE}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE alert SET
                {set_clauses},
                "extra_data" = event::jsonb - '{{{remove_keys}}}'::text[]
            FROM batch
            WHERE alert.id = batch.id
        """))

        if result.rowcount == 0:
            break

        conn.commit()


def downgrade() -> None:
    pass
