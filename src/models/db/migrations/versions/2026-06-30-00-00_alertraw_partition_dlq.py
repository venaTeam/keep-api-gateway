"""SC-05 step 1: partition alertraw (daily) with short-TTL drop retention

Converts `alertraw` from a single unpartitioned heap into a daily range-partitioned
table, with a 7-day retention window. Paired with the application change that makes
alertraw an error-only dead-letter queue (the success-path write was removed in
keep-event-handler), so the table now only ever holds failed-ingest payloads.

Postgres requires the partition key to be part of every unique/primary key, so the PK
becomes composite (id, timestamp). This is mirrored in the ORM models across
keep-api-gateway, keep-event-handler and keep-workflows.

RETENTION MANAGEMENT depends on the environment:
  - If pg_partman is installed (production / target DB, confirmed v5.2.4), the table is
    handed to pg_partman for daily partition pre-creation + drop-based retention.
    pg_partman maintenance (run_maintenance / BGW) must be scheduled for the drop to
    actually fire; this migration only configures it.
  - If pg_partman is NOT installed (e.g. local dev on stock postgres:15), the migration
    still creates the partitioned table with a DEFAULT partition so the table is fully
    usable; automated daily partitioning + retention are skipped in that environment.

SAFETY / DESTRUCTIVENESS:
  - The original table is RENAMED to `alertraw_old`, not dropped, so this migration is
    reversible. After verifying the new partitioned table in production, an operator
    should drop `alertraw_old` manually (it is the multi-TB heap):
        DROP TABLE alertraw_old;
  - Backfill copies only recent ERROR rows (within the 7-day window) into the new
    table — a small, bounded subset, not the whole heap. Older raw payloads are
    intentionally not migrated (short-TTL decision, SC-05 Area 1).

Non-PostgreSQL dialects (sqlite/mysql, e.g. test envs) are skipped — the composite PK
is represented at the model level and applied via metadata there.

Revision ID: alertraw_partition_dlq
Revises: alert_audit_covering_index
Create Date: 2026-06-30
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "alertraw_partition_dlq"
down_revision = "alert_audit_covering_index"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

RETENTION = "7 days"
PARTITION_INTERVAL = "1 day"
PREMAKE = 4  # number of future daily partitions pg_partman keeps ahead


def _partman_schema(connection) -> str | None:
    """Return the schema pg_partman is installed in, or None if not installed."""
    return connection.execute(
        sa.text(
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'pg_partman'"
        )
    ).scalar()


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        logger.info(
            "Skipping alertraw partitioning: only supported on PostgreSQL "
            "(dialect=%s). Composite PK is applied via model metadata.",
            connection.dialect.name,
        )
        return

    # 1. Preserve the existing heap under a new name (reversible; not dropped).
    op.execute("ALTER TABLE alertraw RENAME TO alertraw_old")

    # 2. Create the new partitioned parent with the composite (id, timestamp) PK.
    op.execute(
        """
        CREATE TABLE alertraw (
            id            UUID         NOT NULL DEFAULT gen_random_uuid(),
            tenant_id     VARCHAR      NOT NULL,
            raw_alert     JSONB,
            timestamp     TIMESTAMP    NOT NULL DEFAULT now(),
            provider_type VARCHAR,
            error         BOOLEAN      NOT NULL DEFAULT false,
            error_message VARCHAR,
            dismissed     BOOLEAN      NOT NULL DEFAULT false,
            dismissed_at  TIMESTAMP,
            dismissed_by  VARCHAR,
            PRIMARY KEY (id, timestamp)
        ) PARTITION BY RANGE (timestamp)
        """
    )

    # 3. Indexes (created on the parent; propagated to child partitions).
    op.execute("CREATE INDEX ix_alertraw_tenant_id ON alertraw (tenant_id)")
    op.execute("CREATE INDEX ix_alertraw_error ON alertraw (error)")
    op.execute(
        "CREATE INDEX ix_alert_raw_tenant_id_error ON alertraw (tenant_id, error)"
    )
    op.execute(
        "CREATE INDEX ix_alert_raw_tenant_id_timestamp "
        "ON alertraw (tenant_id, timestamp)"
    )

    # 4. Retention management — pg_partman if available, else a DEFAULT partition.
    partman_schema = _partman_schema(connection)
    if partman_schema:
        connection.execute(
            sa.text(
                f"""
                SELECT {partman_schema}.create_parent(
                    p_parent_table := 'public.alertraw',
                    p_control      := 'timestamp',
                    p_interval     := :interval,
                    p_type         := 'range',
                    p_premake      := :premake
                )
                """
            ).bindparams(interval=PARTITION_INTERVAL, premake=PREMAKE)
        )
        connection.execute(
            sa.text(
                f"""
                UPDATE {partman_schema}.part_config
                SET retention            = :retention,
                    retention_keep_table = false,
                    retention_keep_index = false
                WHERE parent_table = 'public.alertraw'
                """
            ).bindparams(retention=RETENTION)
        )
        logger.info(
            "alertraw handed to pg_partman (schema=%s): daily partitions, %s "
            "drop-retention. Ensure run_maintenance is scheduled.",
            partman_schema,
            RETENTION,
        )
    else:
        op.execute("CREATE TABLE alertraw_default PARTITION OF alertraw DEFAULT")
        logger.warning(
            "pg_partman not installed: created DEFAULT partition only. Automated "
            "daily partitioning and drop-retention are DISABLED in this environment "
            "(expected for local dev on stock postgres)."
        )

    # 5. Backfill only recent error rows (bounded subset, not the whole heap).
    op.execute(
        f"""
        INSERT INTO alertraw (
            id, tenant_id, raw_alert, timestamp, provider_type,
            error, error_message, dismissed, dismissed_at, dismissed_by
        )
        SELECT id, tenant_id, raw_alert, timestamp, provider_type,
               error, error_message, dismissed, dismissed_at, dismissed_by
        FROM alertraw_old
        WHERE error = true
          AND timestamp >= now() - interval '{RETENTION}'
        """
    )

    logger.info(
        "alertraw is now daily-partitioned. Verify in production, then drop the "
        "preserved heap: DROP TABLE alertraw_old;"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        logger.info(
            "Skipping alertraw partitioning downgrade on dialect=%s.",
            connection.dialect.name,
        )
        return

    # Remove pg_partman management (if configured), drop the partitioned table,
    # then restore the original heap.
    partman_schema = _partman_schema(connection)
    if partman_schema:
        connection.execute(
            sa.text(
                f"DELETE FROM {partman_schema}.part_config "
                "WHERE parent_table = 'public.alertraw'"
            )
        )
    op.execute("DROP TABLE IF EXISTS alertraw CASCADE")
    op.execute("ALTER TABLE alertraw_old RENAME TO alertraw")
