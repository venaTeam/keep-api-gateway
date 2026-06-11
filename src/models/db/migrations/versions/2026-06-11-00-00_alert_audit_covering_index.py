"""add covering index on alertaudit (tenant_id, fingerprint, timestamp)

The GET /alerts/{fingerprint}/audit query pattern is:

    WHERE tenant_id = ? AND fingerprint = ?
    ORDER BY timestamp DESC
    LIMIT 50

The existing ix_alert_audit_tenant_id_fingerprint index covers the WHERE clause
but leaves the ORDER BY unsatisfied, forcing Postgres to filter all matching rows
and then sort them.  For a high-volume fingerprint with thousands of audit events
this sort step is the source of intermittent slow responses.

Adding a composite (tenant_id, fingerprint, timestamp) index allows Postgres to do
a single backward index scan — matching the prefix predicates and reading rows in
descending timestamp order — stopping as soon as LIMIT is reached.  No sort step.

Revision ID: alert_audit_covering_index
Revises: drop_alertenrichment
Create Date: 2026-06-11
"""

import sqlalchemy as sa
from alembic import op

revision = "alert_audit_covering_index"
down_revision = "drop_alertenrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_alert_audit_tenant_fingerprint_ts",
        "alertaudit",
        ["tenant_id", "fingerprint", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_audit_tenant_fingerprint_ts", table_name="alertaudit")
