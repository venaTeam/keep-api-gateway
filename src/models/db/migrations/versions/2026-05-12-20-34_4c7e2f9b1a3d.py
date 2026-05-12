"""feat: Phase 1 - add enrichment columns to lastalert and backfill

Revision ID: 4c7e2f9b1a3d
Revises: 9dd1be4539e0
Create Date: 2026-05-12 20:34:00.000000

Phase 1 of the alertenrichment table removal (ALERTENRICHMENT_ROLLOUT_PLAN.md).
Adds typed columns to lastalert for user enrichment state and system tracking fields.
Backfills from alert.event JSON (tracking fields) then alertenrichment.enrichments JSON
(enrichment overrides). Does NOT drop alertenrichment — old code keeps running unchanged.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "4c7e2f9b1a3d"
down_revision = "9dd1be4539e0"
branch_labels = None
depends_on = None

BATCH_SIZE = 1000


def upgrade() -> None:
    conn = op.get_bind()

    # --- Task 1.1: Add new columns to lastalert ---
    with op.batch_alter_table("lastalert", schema=None) as batch_op:
        # User enrichment state (from alertenrichment)
        batch_op.add_column(sa.Column("status", sa.String(50), nullable=True))
        batch_op.add_column(
            sa.Column(
                "status_disposable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )
        batch_op.add_column(sa.Column("dismiss_mode", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("dismissed_until", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("assignee", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("note", sa.Text(), nullable=True))
        # System tracking fields (from alert.event JSON)
        batch_op.add_column(sa.Column("last_received", sa.String(255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "firing_counter",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "unresolved_counter",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("started_at", sa.String(255), nullable=True))
        batch_op.add_column(
            sa.Column("firing_start_time", sa.String(255), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "firing_start_time_since_last_resolved", sa.String(255), nullable=True
            )
        )

    # --- Task 1.2: Add indexes ---
    if conn.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; use autocommit mode.
        with conn.execution_options(isolation_level="AUTOCOMMIT"):
            conn.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lastalert_tenant_status "
                    "ON lastalert (tenant_id, status)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lastalert_tenant_assignee "
                    "ON lastalert (tenant_id, assignee)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lastalert_tenant_dismissed_until "
                    "ON lastalert (tenant_id, dismissed_until)"
                )
            )
    else:
        op.create_index(
            "idx_lastalert_tenant_status", "lastalert", ["tenant_id", "status"]
        )
        op.create_index(
            "idx_lastalert_tenant_assignee", "lastalert", ["tenant_id", "assignee"]
        )
        op.create_index(
            "idx_lastalert_tenant_dismissed_until",
            "lastalert",
            ["tenant_id", "dismissed_until"],
        )

    # --- Task 1.3: Backfill tracking fields from alert.event JSON ---
    _backfill_from_alert(conn)

    # --- Task 1.4: Backfill enrichment state from alertenrichment (overrides 1.3) ---
    _backfill_from_alertenrichment(conn)


def _backfill_from_alert(conn) -> None:
    """Copy system tracking fields from alert.event JSON into lastalert columns.

    Joins lastalert → alert via alert_id. Runs in batches to avoid long-running locks.
    Non-PostgreSQL databases use json_extract syntax.
    """
    total = conn.execute(text("SELECT COUNT(*) FROM lastalert")).scalar() or 0
    if total == 0:
        return

    if conn.dialect.name == "postgresql":
        update_sql = text(
            """
            WITH batch AS (
                SELECT tenant_id, fingerprint
                FROM lastalert
                ORDER BY tenant_id, fingerprint
                LIMIT :batch_size OFFSET :offset
            )
            UPDATE lastalert la
            SET
                last_received                          = a.event->>'lastReceived',
                firing_counter                         = COALESCE(
                                                           NULLIF(a.event->>'firingCounter', '')::integer,
                                                           0),
                unresolved_counter                     = COALESCE(
                                                           NULLIF(a.event->>'unresolvedCounter', '')::integer,
                                                           0),
                started_at                             = a.event->>'startedAt',
                firing_start_time                      = a.event->>'firingStartTime',
                firing_start_time_since_last_resolved  = a.event->>'firingStartTimeSinceLastResolved',
                assignee                               = a.event->>'assignee',
                dismissed_until                        = CASE
                                                           WHEN a.event->>'dismissUntil' IS NOT NULL
                                                             AND a.event->>'dismissUntil' <> ''
                                                           THEN (a.event->>'dismissUntil')::timestamp
                                                           ELSE NULL
                                                         END,
                note                                   = a.event->>'note'
            FROM alert a, batch b
            WHERE la.tenant_id  = b.tenant_id
              AND la.fingerprint = b.fingerprint
              AND la.alert_id    = a.id
              AND la.tenant_id   = a.tenant_id
            """
        )
    else:
        update_sql = text(
            """
            UPDATE lastalert
            SET
                last_received                          = (SELECT json_extract(a.event, '$.lastReceived')
                                                          FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                firing_counter                         = COALESCE(
                                                           (SELECT CAST(json_extract(a.event, '$.firingCounter') AS INTEGER)
                                                            FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                                                           0),
                unresolved_counter                     = COALESCE(
                                                           (SELECT CAST(json_extract(a.event, '$.unresolvedCounter') AS INTEGER)
                                                            FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                                                           0),
                started_at                             = (SELECT json_extract(a.event, '$.startedAt')
                                                          FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                firing_start_time                      = (SELECT json_extract(a.event, '$.firingStartTime')
                                                          FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                firing_start_time_since_last_resolved  = (SELECT json_extract(a.event, '$.firingStartTimeSinceLastResolved')
                                                          FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                assignee                               = (SELECT json_extract(a.event, '$.assignee')
                                                          FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1),
                note                                   = (SELECT json_extract(a.event, '$.note')
                                                          FROM alert a WHERE a.id = lastalert.alert_id LIMIT 1)
            WHERE lastalert.rowid IN (
                SELECT rowid FROM lastalert
                ORDER BY tenant_id, fingerprint
                LIMIT :batch_size OFFSET :offset
            )
            """
        )

    offset = 0
    while offset < total:
        conn.execute(update_sql, {"batch_size": BATCH_SIZE, "offset": offset})
        offset += BATCH_SIZE


def _backfill_from_alertenrichment(conn) -> None:
    """Copy enrichment state from alertenrichment.enrichments JSON into lastalert columns.

    Applies on top of the alert backfill (task 1.3). Translates the legacy
    dismissed/dismissUntil pattern into status='suppressed' + dismiss_mode + dismissed_until.
    Discards disposable_* keys and any other dynamic custom fields.
    """
    total = conn.execute(text("SELECT COUNT(*) FROM lastalert")).scalar() or 0
    if total == 0:
        return

    if conn.dialect.name == "postgresql":
        update_sql = text(
            """
            WITH batch AS (
                SELECT tenant_id, fingerprint
                FROM lastalert
                ORDER BY tenant_id, fingerprint
                LIMIT :batch_size OFFSET :offset
            )
            UPDATE lastalert la
            SET
                status          = CASE
                                    WHEN ae.enrichments->>'dismissed' = 'true'
                                      THEN 'suppressed'
                                    WHEN ae.enrichments->>'status' IS NOT NULL
                                      AND ae.enrichments->>'status' <> ''
                                      THEN ae.enrichments->>'status'
                                    ELSE la.status
                                  END,
                dismiss_mode    = CASE
                                    WHEN ae.enrichments->>'dismissed' = 'true'
                                     AND ae.enrichments->>'dismissUntil' IS NOT NULL
                                     AND ae.enrichments->>'dismissUntil' <> ''
                                      THEN 'dismiss_until'
                                    WHEN ae.enrichments->>'dismissed' = 'true'
                                      THEN 'permanent'
                                    ELSE NULL
                                  END,
                dismissed_until = CASE
                                    WHEN ae.enrichments->>'dismissUntil' IS NOT NULL
                                     AND ae.enrichments->>'dismissUntil' <> ''
                                      THEN (ae.enrichments->>'dismissUntil')::timestamp
                                    ELSE la.dismissed_until
                                  END,
                assignee        = COALESCE(
                                    NULLIF(ae.enrichments->>'assignee', ''),
                                    la.assignee),
                note            = COALESCE(
                                    NULLIF(ae.enrichments->>'note', ''),
                                    la.note)
            FROM alertenrichment ae, batch b
            WHERE la.tenant_id  = b.tenant_id
              AND la.fingerprint = b.fingerprint
              AND la.fingerprint = ae.alert_fingerprint
              AND la.tenant_id   = ae.tenant_id
            """
        )
    else:
        update_sql = text(
            """
            UPDATE lastalert
            SET
                status = CASE
                           WHEN (SELECT json_extract(ae.enrichments, '$.dismissed')
                                 FROM alertenrichment ae
                                 WHERE ae.alert_fingerprint = lastalert.fingerprint
                                   AND ae.tenant_id = lastalert.tenant_id
                                 LIMIT 1) = 'true'
                             THEN 'suppressed'
                           ELSE COALESCE(
                                  (SELECT NULLIF(json_extract(ae.enrichments, '$.status'), '')
                                   FROM alertenrichment ae
                                   WHERE ae.alert_fingerprint = lastalert.fingerprint
                                     AND ae.tenant_id = lastalert.tenant_id
                                   LIMIT 1),
                                  lastalert.status)
                         END,
                dismiss_mode = CASE
                                 WHEN (SELECT json_extract(ae.enrichments, '$.dismissed')
                                       FROM alertenrichment ae
                                       WHERE ae.alert_fingerprint = lastalert.fingerprint
                                         AND ae.tenant_id = lastalert.tenant_id
                                       LIMIT 1) = 'true'
                                  AND (SELECT NULLIF(json_extract(ae.enrichments, '$.dismissUntil'), '')
                                       FROM alertenrichment ae
                                       WHERE ae.alert_fingerprint = lastalert.fingerprint
                                         AND ae.tenant_id = lastalert.tenant_id
                                       LIMIT 1) IS NOT NULL
                                   THEN 'dismiss_until'
                                 WHEN (SELECT json_extract(ae.enrichments, '$.dismissed')
                                       FROM alertenrichment ae
                                       WHERE ae.alert_fingerprint = lastalert.fingerprint
                                         AND ae.tenant_id = lastalert.tenant_id
                                       LIMIT 1) = 'true'
                                   THEN 'permanent'
                                 ELSE NULL
                               END,
                dismissed_until = (SELECT NULLIF(json_extract(ae.enrichments, '$.dismissUntil'), '')
                                   FROM alertenrichment ae
                                   WHERE ae.alert_fingerprint = lastalert.fingerprint
                                     AND ae.tenant_id = lastalert.tenant_id
                                   LIMIT 1),
                assignee = COALESCE(
                             (SELECT NULLIF(json_extract(ae.enrichments, '$.assignee'), '')
                              FROM alertenrichment ae
                              WHERE ae.alert_fingerprint = lastalert.fingerprint
                                AND ae.tenant_id = lastalert.tenant_id
                              LIMIT 1),
                             lastalert.assignee),
                note = COALESCE(
                         (SELECT NULLIF(json_extract(ae.enrichments, '$.note'), '')
                          FROM alertenrichment ae
                          WHERE ae.alert_fingerprint = lastalert.fingerprint
                            AND ae.tenant_id = lastalert.tenant_id
                          LIMIT 1),
                         lastalert.note)
            WHERE lastalert.rowid IN (
                SELECT rowid FROM lastalert
                ORDER BY tenant_id, fingerprint
                LIMIT :batch_size OFFSET :offset
            )
            """
        )

    offset = 0
    while offset < total:
        conn.execute(update_sql, {"batch_size": BATCH_SIZE, "offset": offset})
        offset += BATCH_SIZE


def downgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        with conn.execution_options(isolation_level="AUTOCOMMIT"):
            conn.execute(
                text("DROP INDEX CONCURRENTLY IF EXISTS idx_lastalert_tenant_status")
            )
            conn.execute(
                text("DROP INDEX CONCURRENTLY IF EXISTS idx_lastalert_tenant_assignee")
            )
            conn.execute(
                text(
                    "DROP INDEX CONCURRENTLY IF EXISTS idx_lastalert_tenant_dismissed_until"
                )
            )
    else:
        op.drop_index("idx_lastalert_tenant_status", table_name="lastalert")
        op.drop_index("idx_lastalert_tenant_assignee", table_name="lastalert")
        op.drop_index("idx_lastalert_tenant_dismissed_until", table_name="lastalert")

    with op.batch_alter_table("lastalert", schema=None) as batch_op:
        batch_op.drop_column("firing_start_time_since_last_resolved")
        batch_op.drop_column("firing_start_time")
        batch_op.drop_column("started_at")
        batch_op.drop_column("unresolved_counter")
        batch_op.drop_column("firing_counter")
        batch_op.drop_column("last_received")
        batch_op.drop_column("note")
        batch_op.drop_column("assignee")
        batch_op.drop_column("dismissed_until")
        batch_op.drop_column("dismiss_mode")
        batch_op.drop_column("status_disposable")
        batch_op.drop_column("status")
