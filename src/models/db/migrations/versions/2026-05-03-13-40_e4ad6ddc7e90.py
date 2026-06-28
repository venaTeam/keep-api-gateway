"""add_alert_columns

Revision ID: e4ad6ddc7e90
Revises: 9dd1be4539e0
Create Date: 2026-05-03 13:40:26.861963

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

# revision identifiers, used by Alembic.
revision = "e4ad6ddc7e90"
down_revision = "9dd1be4539e0"
branch_labels = None
depends_on = None


# NOTE: this migration historically introspected the live ``Alert`` model
# (``for col in Alert.__table__.columns``). That made the chain
# non-deterministic: once the model moved last_received / note / assignee /
# incident / dismissUntil onto ``LastAlert`` (snake_case), a clean replay no
# longer created the camelCase payload columns the downstream fixed-DDL
# migrations (20d5647ff365 rename, a1b2c3d4e5f6 timestamptz conversion, ...)
# depend on, so replay-from-base crashed with
# ``column "last_received" does not exist``.
#
# The column set is now FROZEN to the Alert model as of the revision that
# introduced this migration (commit 589070e, 2026-05-06): the camelCase
# payload columns plus the transient overflow columns. Migrations must never
# import live models. Adds are guarded so this is a no-op on databases that
# already carry these columns (e.g. ones patched out-of-band).

# (name, type) frozen from the 2026-05-06 Alert model. All added nullable to
# avoid NotNullViolation on existing rows; real nullability is enforced later.
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
    ("enriched_fields", PG_JSONB()),
    ("startedAt", sa.String(255)),
    ("firingCounter", sa.Integer()),
    ("unresolvedCounter", sa.Integer()),
    ("firingStartTime", sa.String(255)),
    ("firingStartTimeSinceLastResolved", sa.String(255)),
    # transient overflow column, backfilled by 5dfe012ad560 and dropped by e1932
    ("extra_data", PG_JSONB()),
]


def _existing_alert_columns():
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("alert")}


def upgrade() -> None:
    existing = _existing_alert_columns()
    for name, type_ in _PAYLOAD_COLUMNS:
        if name not in existing:
            op.add_column("alert", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    existing = _existing_alert_columns()
    for name, _type in _PAYLOAD_COLUMNS:
        if name in existing:
            op.drop_column("alert", name)
