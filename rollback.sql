-- =========================================================================
-- Rollback: re-pack the flat columns back into alert.event JSONB
-- and drop the flat columns.
--
-- Designed to run cleanly in pgAdmin with auto-commit:
--   - Run STEP 0, STEP 1, and STEP 2 once. They are idempotent.
--   - Run STEP 3 (the batch statement) REPEATEDLY. Each execution
--     rebuilds up to 10 000 alerts' event blob in a single
--     auto-committed transaction, logs them to alert_migration_log,
--     and stops including them in the next batch. Keep clicking Run
--     until pgAdmin reports "UPDATE 0".
--   - STEP 5 drops the flat columns; only run after STEP 3 is done.
-- =========================================================================


-- STEP 0: Audit log table (same table the rollout uses)
CREATE TABLE IF NOT EXISTS alert_migration_log (
    log_id          BIGSERIAL    PRIMARY KEY,
    direction       VARCHAR(16)  NOT NULL,
    alert_id        UUID         NOT NULL,
    fingerprint     VARCHAR(255),
    tenant_id       VARCHAR(255),
    batch_number    INT          NOT NULL,
    migrated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status          VARCHAR(20)  NOT NULL,
    error_message   TEXT,
    original_event  JSONB
);

CREATE INDEX IF NOT EXISTS ix_alert_migration_log_alert    ON alert_migration_log(alert_id);
CREATE INDEX IF NOT EXISTS ix_alert_migration_log_dir_stat ON alert_migration_log(direction, status);
CREATE INDEX IF NOT EXISTS ix_alert_migration_log_batch    ON alert_migration_log(direction, batch_number);


-- STEP 1: Restore the event column (extra_data is no longer used)
ALTER TABLE alert ADD COLUMN IF NOT EXISTS event JSONB;


-- STEP 2: Sequence used to number batches across multiple runs (idempotent)
CREATE SEQUENCE IF NOT EXISTS alert_migration_rollback_batch_seq;


-- =========================================================================
-- STEP 3: PROCESS ONE BATCH OF UP TO 10 000 ALERTS.
-- Re-run this statement until pgAdmin reports "UPDATE 0".
-- Single statement -> single transaction -> works with auto-commit.
-- =========================================================================
WITH batch AS (
    SELECT a.id, a.fingerprint, a.tenant_id,
           jsonb_build_object(
               'application',                       a.application,
               'object',                            a.object,
               'node_name',                         a.node_name,
               'severity',                          a.severity,
               'message',                           a.message,
               'operator',                          a.operator,
               'time_created',                      a.time_created,
               'network',                           a.network,
               'timezone',                          a.timezone,
               'custom_key',                        a.custom_key,
               'expiry_in_minutes',                 a.expiry_in_minutes,
               'source',                            a.source,
               'service',                           a.service,
               'key_field',                         a.key_field,
               'name',                              a.name,
               'status',                            a.status,
               'description',                       a.description,
               'lastReceived',                      a.last_received,
               'isFullDuplicate',                   a.is_full_duplicate,
               'isPartialDuplicate',                a.is_partial_duplicate,
               'duplicateReason',                   a.duplicate_reason,
               'note',                              a.note,
               'assignee',                          a.assignee,
               'incident',                          a.incident,
               'dismissUntil',                      a.dismiss_until,
               'dismissed',                         a.dismissed,
               'startedAt',                         a.started_at,
               'firingCounter',                     a.firing_counter,
               'unresolvedCounter',                 a.unresolved_counter,
               'firingStartTime',                   a.firing_start_time,
               'firingStartTimeSinceLastResolved',  a.firing_start_time_since_last_resolved
           ) AS rebuilt_event
    FROM alert a
    WHERE a.event IS NULL
      AND (a.service IS NOT NULL OR a.name IS NOT NULL)
      AND NOT EXISTS (
          SELECT 1 FROM alert_migration_log l
          WHERE l.direction = 'rollback' AND l.alert_id = a.id
      )
    ORDER BY a.id
    LIMIT 10000
    FOR UPDATE SKIP LOCKED
),
batch_num AS (
    SELECT nextval('alert_migration_rollback_batch_seq')::INT AS n
),
log_insert AS (
    INSERT INTO alert_migration_log
        (direction, alert_id, fingerprint, tenant_id, batch_number, status, original_event)
    SELECT 'rollback', b.id, b.fingerprint, b.tenant_id, bn.n, 'success', b.rebuilt_event
    FROM batch b CROSS JOIN batch_num bn
    RETURNING alert_id
)
UPDATE alert AS a
SET event = batch.rebuilt_event
FROM batch
WHERE a.id = batch.id;


-- =========================================================================
-- STEP 4: Verification queries (run any time)
--
--   -- Total restored so far:
--   SELECT COUNT(*) FROM alert_migration_log WHERE direction = 'rollback';
--
--   -- Remaining un-restored rows:
--   SELECT COUNT(*) FROM alert WHERE event IS NULL AND name IS NOT NULL;
--
--   -- Per-batch summary:
--   SELECT batch_number, COUNT(*) AS rows, MIN(migrated_at) AS started
--   FROM alert_migration_log
--   WHERE direction = 'rollback'
--   GROUP BY batch_number
--   ORDER BY batch_number;
--
--   -- Inspect one alert's rebuilt event payload:
--   SELECT alert_id, original_event
--   FROM alert_migration_log
--   WHERE direction = 'rollback' AND alert_id = '<your-uuid>';
-- =========================================================================


-- STEP 5: Drop the flat columns (only run after STEP 3 reports UPDATE 0).
ALTER TABLE alert
    DROP COLUMN IF EXISTS application,
    DROP COLUMN IF EXISTS object,
    DROP COLUMN IF EXISTS node_name,
    DROP COLUMN IF EXISTS severity,
    DROP COLUMN IF EXISTS message,
    DROP COLUMN IF EXISTS operator,
    DROP COLUMN IF EXISTS time_created,
    DROP COLUMN IF EXISTS network,
    DROP COLUMN IF EXISTS timezone,
    DROP COLUMN IF EXISTS custom_key,
    DROP COLUMN IF EXISTS expiry_in_minutes,
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS service,
    DROP COLUMN IF EXISTS key_field,
    DROP COLUMN IF EXISTS name,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS description,
    DROP COLUMN IF EXISTS last_received,
    DROP COLUMN IF EXISTS is_full_duplicate,
    DROP COLUMN IF EXISTS is_partial_duplicate,
    DROP COLUMN IF EXISTS duplicate_reason,
    DROP COLUMN IF EXISTS note,
    DROP COLUMN IF EXISTS assignee,
    DROP COLUMN IF EXISTS incident,
    DROP COLUMN IF EXISTS dismiss_until,
    DROP COLUMN IF EXISTS dismissed,
    DROP COLUMN IF EXISTS started_at,
    DROP COLUMN IF EXISTS firing_counter,
    DROP COLUMN IF EXISTS unresolved_counter,
    DROP COLUMN IF EXISTS firing_start_time,
    DROP COLUMN IF EXISTS firing_start_time_since_last_resolved;
