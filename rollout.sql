-- =========================================================================
-- Rollout: move alert.event (JSONB blob) into native flat columns.
--
-- Designed to run cleanly in pgAdmin with auto-commit:
--   - Run STEP 0, STEP 1, and STEP 2 once. They are idempotent.
--   - Run STEP 3 (the batch statement) REPEATEDLY. Each execution
--     processes up to 10 000 alerts in a single auto-committed
--     transaction, logs them to alert_migration_log, and clears
--     their `event` blob. Keep clicking Run until pgAdmin reports
--     "UPDATE 0" (or the verification query in STEP 4 shows 0
--     remaining rows).
--   - STEP 5 is optional final cleanup; only run after verification.
-- =========================================================================


-- STEP 0: Audit log table (one row per migrated alert)
CREATE TABLE IF NOT EXISTS alert_migration_log (
    log_id          BIGSERIAL    PRIMARY KEY,
    direction       VARCHAR(16)  NOT NULL,           -- 'rollout' or 'rollback'
    alert_id        UUID         NOT NULL,
    fingerprint     VARCHAR(255),
    tenant_id       VARCHAR(255),
    batch_number    INT          NOT NULL,
    migrated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status          VARCHAR(20)  NOT NULL,           -- 'success' (slot for future error rows)
    error_message   TEXT,
    -- Snapshot of the original 'event' JSON so individual alerts can be
    -- inspected or recovered after the fact.
    original_event  JSONB
);

CREATE INDEX IF NOT EXISTS ix_alert_migration_log_alert    ON alert_migration_log(alert_id);
CREATE INDEX IF NOT EXISTS ix_alert_migration_log_dir_stat ON alert_migration_log(direction, status);
CREATE INDEX IF NOT EXISTS ix_alert_migration_log_batch    ON alert_migration_log(direction, batch_number);


-- STEP 1: Add the new flat columns (idempotent)
ALTER TABLE alert ADD COLUMN IF NOT EXISTS application VARCHAR(200);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS object VARCHAR(200);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS node_name VARCHAR(200);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS severity VARCHAR(50);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS message VARCHAR(800);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS operator VARCHAR(100);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS time_created VARCHAR(50);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS network VARCHAR(50) DEFAULT 'nh';
ALTER TABLE alert ADD COLUMN IF NOT EXISTS timezone VARCHAR(50) DEFAULT 'Asia/Jerusalem';
ALTER TABLE alert ADD COLUMN IF NOT EXISTS custom_key VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS expiry_in_minutes INTEGER;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS source VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS service VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS key_field VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS name VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS status VARCHAR(50);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS last_received VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS is_full_duplicate BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS is_partial_duplicate BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS duplicate_reason VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS assignee VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS incident VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS dismiss_until VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS dismissed BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS started_at VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS firing_counter INTEGER DEFAULT 0;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS unresolved_counter INTEGER DEFAULT 0;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS firing_start_time VARCHAR(255);
ALTER TABLE alert ADD COLUMN IF NOT EXISTS firing_start_time_since_last_resolved VARCHAR(255);


-- STEP 2: Sequence used to number batches across multiple runs (idempotent)
CREATE SEQUENCE IF NOT EXISTS alert_migration_rollout_batch_seq;


-- =========================================================================
-- STEP 3: PROCESS ONE BATCH OF UP TO 10 000 ALERTS.
-- Re-run this statement until pgAdmin reports "UPDATE 0".
-- Single statement -> single transaction -> works with auto-commit.
-- =========================================================================
WITH batch AS (
    SELECT a.id, a.fingerprint, a.tenant_id, a.event
    FROM alert a
    WHERE a.event IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM alert_migration_log l
          WHERE l.direction = 'rollout' AND l.alert_id = a.id
      )
    ORDER BY a.id
    LIMIT 10000
    FOR UPDATE SKIP LOCKED
),
batch_num AS (
    SELECT nextval('alert_migration_rollout_batch_seq')::INT AS n
),
log_insert AS (
    INSERT INTO alert_migration_log
        (direction, alert_id, fingerprint, tenant_id, batch_number, status, original_event)
    SELECT 'rollout', b.id, b.fingerprint, b.tenant_id, bn.n, 'success', b.event
    FROM batch b CROSS JOIN batch_num bn
    RETURNING alert_id
)
UPDATE alert AS a
SET
    application                            = batch.event->>'application',
    object                                 = batch.event->>'object',
    node_name                              = batch.event->>'node_name',
    severity                               = batch.event->>'severity',
    message                                = batch.event->>'message',
    operator                               = batch.event->>'operator',
    time_created                           = batch.event->>'time_created',
    network                                = COALESCE(batch.event->>'network', 'nh'),
    timezone                               = COALESCE(batch.event->>'timezone', 'Asia/Jerusalem'),
    custom_key                             = batch.event->>'custom_key',
    expiry_in_minutes                      = NULLIF(batch.event->>'expiry_in_minutes', '')::INTEGER,
    source                                 = batch.event->>'source',
    service                                = batch.event->>'service',
    key_field                              = batch.event->>'key_field',
    name                                   = batch.event->>'name',
    status                                 = batch.event->>'status',
    description                            = batch.event->>'description',
    last_received                          = batch.event->>'lastReceived',
    is_full_duplicate                      = NULLIF(batch.event->>'isFullDuplicate', '')::BOOLEAN,
    is_partial_duplicate                   = NULLIF(batch.event->>'isPartialDuplicate', '')::BOOLEAN,
    duplicate_reason                       = batch.event->>'duplicateReason',
    note                                   = batch.event->>'note',
    assignee                               = batch.event->>'assignee',
    incident                               = batch.event->>'incident',
    dismiss_until                          = batch.event->>'dismissUntil',
    dismissed                              = COALESCE(NULLIF(batch.event->>'dismissed', '')::BOOLEAN, FALSE),
    started_at                             = batch.event->>'startedAt',
    firing_counter                         = COALESCE(NULLIF(batch.event->>'firingCounter', '')::INTEGER, 0),
    unresolved_counter                     = COALESCE(NULLIF(batch.event->>'unresolvedCounter', '')::INTEGER, 0),
    firing_start_time                      = batch.event->>'firingStartTime',
    firing_start_time_since_last_resolved  = batch.event->>'firingStartTimeSinceLastResolved'
FROM batch
WHERE a.id = batch.id;


-- =========================================================================
-- STEP 4: Verification queries (run any time)
--
--   -- Total migrated so far:
--   SELECT COUNT(*) FROM alert_migration_log WHERE direction = 'rollout';
--
--   -- Remaining un-migrated rows (alerts with event but no rollout log row):
--   SELECT COUNT(*) FROM alert a
--   WHERE a.event IS NOT NULL
--     AND NOT EXISTS (
--         SELECT 1 FROM alert_migration_log l
--         WHERE l.direction = 'rollout' AND l.alert_id = a.id
--     );
--
--   -- Per-batch summary:
--   SELECT batch_number, COUNT(*) AS rows, MIN(migrated_at) AS started
--   FROM alert_migration_log
--   WHERE direction = 'rollout'
--   GROUP BY batch_number
--   ORDER BY batch_number;
--
--   -- Spot-check: compare flat columns against the still-present event blob:
--   SELECT id, name, severity, last_received,
--          event->>'name'         AS event_name,
--          event->>'severity'     AS event_severity,
--          event->>'lastReceived' AS event_last_received
--   FROM alert
--   WHERE id = '<your-uuid>';
--
--   -- Inspect a logged snapshot:
--   SELECT alert_id, original_event
--   FROM alert_migration_log
--   WHERE direction = 'rollout' AND alert_id = '<your-uuid>';
-- =========================================================================


-- STEP 5 (only after verification): drop the now-redundant event column.
-- This is the irreversible step that actually reclaims storage. Run it
-- after spot-checking that the flat columns look right.
-- ALTER TABLE alert DROP COLUMN event;
