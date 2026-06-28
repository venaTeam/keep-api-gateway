"""drop_event_column

Revision ID: 2b4dd4b88121
Revises: 5dfe012ad560
Create Date: 2026-05-03 14:02:01.428644

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

# revision identifiers, used by Alembic.
revision = "2b4dd4b88121"
down_revision = "5dfe012ad560"
branch_labels = None
depends_on = None


# Payload column list FROZEN to the Alert model as of the revision that
# introduced this migration (commit 589070e, 2026-05-06) instead of
# introspecting the live model. The upgrade only drops ``event``; the frozen
# list is for the downgrade's reconstruction of the ``event`` JSON, and must
# match the columns e4ad6ddc7e90 created. Migrations must not import live models.
_PAYLOAD_COLUMNS = [
    "application", "object", "node_name", "severity", "message", "operator",
    "time_created", "network", "timezone", "custom_key", "expiry_in_minutes",
    "source", "service", "key_field", "name", "status", "description",
    "lastReceived", "isFullDuplicate", "isPartialDuplicate", "duplicateReason",
    "note", "assignee", "incident", "dismissUntil", "dismissed",
    "enriched_fields", "startedAt", "firingCounter", "unresolvedCounter",
    "firingStartTime", "firingStartTimeSinceLastResolved",
]


def _alert_columns():
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("alert")}


def upgrade() -> None:
    if "event" in _alert_columns():
        op.drop_column("alert", "event")


def downgrade() -> None:
    if "event" not in _alert_columns():
        op.add_column("alert", sa.Column("event", PG_JSONB, nullable=True))

    existing = _alert_columns()
    payload_cols = [c for c in _PAYLOAD_COLUMNS if c in existing]
    json_build = ", ".join(f"'{col}', \"{col}\"" for col in payload_cols)
    op.execute(f"""
        UPDATE alert SET event = jsonb_build_object({json_build})
    """)
