"""SC-05 step 2: add alertdeduplicationevent timestamp index (partition key)

Part of SC-05 (DB growth). Adds a plain index on `alertdeduplicationevent.timestamp`,
the future daily-partition key. As with step 1, the actual partitioning of the table
(range-partition by timestamp, composite (id, timestamp) PK, drop-based retention) is
performed by the DBA out-of-band, because that DDL needs elevated permissions the
application role does not have. The app-side scope is this index only.

`timestamp` is chosen over `date_hour` as the partition key because it is NOT NULL
(date_hour is nullable), it is uniform with the other SC-05 partitioned tables
(alert / alertaudit / alertraw all partition on `timestamp`), and it is the natural
row-age column for retention. The existing date_hour / provider indexes are kept for
the dedup-distribution analytics queries.

Revision ID: dedup_event_timestamp_index
Revises: alertraw_partition_dlq
Create Date: 2026-07-12
"""

from alembic import op

revision = "dedup_event_timestamp_index"
down_revision = "alertraw_partition_dlq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_alert_deduplication_event_timestamp",
        "alertdeduplicationevent",
        ["timestamp"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alert_deduplication_event_timestamp",
        table_name="alertdeduplicationevent",
    )
