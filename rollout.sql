-- STEP 1: Add the new columns (Instant)
ALTER TABLE alert ADD COLUMN application VARCHAR(200);
ALTER TABLE alert ADD COLUMN object VARCHAR(200);
ALTER TABLE alert ADD COLUMN node_name VARCHAR(200);
ALTER TABLE alert ADD COLUMN severity VARCHAR(50);
ALTER TABLE alert ADD COLUMN message VARCHAR(800);
ALTER TABLE alert ADD COLUMN operator VARCHAR(100);
ALTER TABLE alert ADD COLUMN time_created VARCHAR(50);
ALTER TABLE alert ADD COLUMN network VARCHAR(50) DEFAULT 'nh';
ALTER TABLE alert ADD COLUMN timezone VARCHAR(50) DEFAULT 'Asia/Jerusalem';
ALTER TABLE alert ADD COLUMN custom_key VARCHAR(255);
ALTER TABLE alert ADD COLUMN expiry_in_minutes INTEGER;
ALTER TABLE alert ADD COLUMN source VARCHAR(255);
ALTER TABLE alert ADD COLUMN service VARCHAR(255);
ALTER TABLE alert ADD COLUMN key_field VARCHAR(255);
ALTER TABLE alert ADD COLUMN name VARCHAR(255);
ALTER TABLE alert ADD COLUMN status VARCHAR(50);
ALTER TABLE alert ADD COLUMN description TEXT;
ALTER TABLE alert ADD COLUMN last_received VARCHAR(255);
ALTER TABLE alert ADD COLUMN is_full_duplicate BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN is_partial_duplicate BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN duplicate_reason VARCHAR(255);
ALTER TABLE alert ADD COLUMN note TEXT;
ALTER TABLE alert ADD COLUMN assignee VARCHAR(255);
ALTER TABLE alert ADD COLUMN incident VARCHAR(255);
ALTER TABLE alert ADD COLUMN dismiss_until VARCHAR(255);
ALTER TABLE alert ADD COLUMN dismissed BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN started_at VARCHAR(255);
ALTER TABLE alert ADD COLUMN firing_counter INTEGER DEFAULT 0;
ALTER TABLE alert ADD COLUMN unresolved_counter INTEGER DEFAULT 0;
ALTER TABLE alert ADD COLUMN firing_start_time VARCHAR(255);
ALTER TABLE alert ADD COLUMN firing_start_time_since_last_resolved VARCHAR(255);
ALTER TABLE alert ADD COLUMN previous_status VARCHAR(50);
ALTER TABLE alert ADD COLUMN maintenance_windows_trace JSONB;

-- STEP 2: Backfill data (Batched, Safe)
DO $$
DECLARE
    rows_affected INT;
BEGIN
    LOOP
        UPDATE alert
        SET 
            application = event->>'application',
            object = event->>'object',
            node_name = event->>'node_name',
            severity = event->>'severity',
            message = event->>'message',
            operator = event->>'operator',
            time_created = event->>'time_created',
            network = COALESCE(event->>'network', 'nh'),
            timezone = COALESCE(event->>'timezone', 'Asia/Jerusalem'),
            custom_key = event->>'custom_key',
            expiry_in_minutes = (event->>'expiry_in_minutes')::INTEGER,
            source = event->>'source',
            service = event->>'service',
            key_field = event->>'key_field',
            name = event->>'name',
            status = event->>'status',
            description = event->>'description',
            last_received = event->>'lastReceived',
            is_full_duplicate = (event->>'isFullDuplicate')::BOOLEAN,
            is_partial_duplicate = (event->>'isPartialDuplicate')::BOOLEAN,
            duplicate_reason = event->>'duplicateReason',
            note = event->>'note',
            assignee = event->>'assignee',
            incident = event->>'incident',
            dismiss_until = event->>'dismissUntil',
            dismissed = (event->>'dismissed')::BOOLEAN,
            started_at = event->>'startedAt',
            firing_counter = (event->>'firingCounter')::INTEGER,
            unresolved_counter = (event->>'unresolvedCounter')::INTEGER,
            firing_start_time = event->>'firingStartTime',
            firing_start_time_since_last_resolved = event->>'firingStartTimeSinceLastResolved',
            -- Move everything else into extra_data
            extra_data = event - '{application, object, node_name, severity, message, operator, time_created, network, timezone, custom_key, expiry_in_minutes, source, service, key_field, name, status, description, lastReceived, isFullDuplicate, isPartialDuplicate, duplicateReason, note, assignee, incident, dismissUntil, dismissed, startedAt, firingCounter, unresolvedCounter, firingStartTime, firingStartTimeSinceLastResolved}'::text[]
        WHERE id IN (
            SELECT id FROM alert 
            WHERE event IS NOT NULL AND service IS NULL 
            LIMIT 10000
        );

        GET DIAGNOSTICS rows_affected = ROW_COUNT;
        EXIT WHEN rows_affected = 0;
        COMMIT; 
    END LOOP;
END $$;

-- STEP 3: Cleanup (Optional - Run only after verification)
-- ALTER TABLE alert DROP COLUMN event;
