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
ALTER TABLE alert ADD COLUMN lastReceived VARCHAR(255);
ALTER TABLE alert ADD COLUMN isFullDuplicate BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN isPartialDuplicate BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN duplicateReason VARCHAR(255);
ALTER TABLE alert ADD COLUMN note TEXT;
ALTER TABLE alert ADD COLUMN assignee VARCHAR(255);
ALTER TABLE alert ADD COLUMN incident VARCHAR(255);
ALTER TABLE alert ADD COLUMN dismissUntil VARCHAR(255);
ALTER TABLE alert ADD COLUMN dismissed BOOLEAN DEFAULT FALSE;
ALTER TABLE alert ADD COLUMN startedAt VARCHAR(255);
ALTER TABLE alert ADD COLUMN firingCounter INTEGER DEFAULT 0;
ALTER TABLE alert ADD COLUMN unresolvedCounter INTEGER DEFAULT 0;
ALTER TABLE alert ADD COLUMN firingStartTime VARCHAR(255);
ALTER TABLE alert ADD COLUMN firingStartTimeSinceLastResolved VARCHAR(255);

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
            lastReceived = event->>'lastReceived',
            isFullDuplicate = (event->>'isFullDuplicate')::BOOLEAN,
            isPartialDuplicate = (event->>'isPartialDuplicate')::BOOLEAN,
            duplicateReason = event->>'duplicateReason',
            note = event->>'note',
            assignee = event->>'assignee',
            incident = event->>'incident',
            dismissUntil = event->>'dismissUntil',
            dismissed = (event->>'dismissed')::BOOLEAN,
            startedAt = event->>'startedAt',
            firingCounter = (event->>'firingCounter')::INTEGER,
            unresolvedCounter = (event->>'unresolvedCounter')::INTEGER,
            firingStartTime = event->>'firingStartTime',
            firingStartTimeSinceLastResolved = event->>'firingStartTimeSinceLastResolved',
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
