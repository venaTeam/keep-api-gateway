"""SC-05 step 1: add alertraw timestamp index (partition key)

Part of SC-05 (DB growth). This migration is intentionally minimal: it only adds a
plain index on `alertraw.timestamp`.

Partitioning of `alertraw` (converting it to a daily range-partitioned table with
short-TTL, drop-based retention) is performed by the DBA out-of-band, because that
DDL requires elevated permissions the application role does not have. The app-side
scope of SC-05 step 1 is therefore:

  1. alertraw is written only for failed events (error-only DLQ) — see the change in
     keep-event-handler (__save_to_db no longer stores successful payloads).
  2. This index on `timestamp` (the future partition key) — supports the time-range
     scans the DBA's retention/partition-pruning will rely on, and is a no-op to keep
     once the table is partitioned.

`create_index` works on every supported dialect, so no dialect guard is needed.

Revision ID: alertraw_partition_dlq
Revises: alert_audit_covering_index
Create Date: 2026-06-30
"""

from alembic import op

revision = "alertraw_partition_dlq"
down_revision = "alert_audit_covering_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_alertraw_timestamp", "alertraw", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_alertraw_timestamp", table_name="alertraw")
