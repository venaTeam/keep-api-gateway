-- STEP 1: Restore the event column (if dropped)
ALTER TABLE alert ADD COLUMN IF NOT EXISTS event JSONB;

-- STEP 2: Re-populate event column from flat columns and extra_data (Safe)
DO $$
DECLARE
    rows_affected INT;
BEGIN
    LOOP
        UPDATE alert
        SET event = jsonb_build_object(
            'application', application,
            'object', object,
            'node_name', node_name,
            'severity', severity,
            'message', message,
            'operator', operator,
            'time_created', time_created,
            'network', network,
            'timezone', timezone,
            'custom_key', custom_key,
            'expiry_in_minutes', expiry_in_minutes,
            'source', source,
            'service', service,
            'key_field', key_field,
            'name', name,
            'status', status,
            'description', description,
            'lastReceived', lastReceived,
            'isFullDuplicate', isFullDuplicate,
            'isPartialDuplicate', isPartialDuplicate,
            'duplicateReason', duplicateReason,
            'note', note,
            'assignee', assignee,
            'incident', incident,
            'dismissUntil', dismissUntil,
            'dismissed', dismissed,
            'startedAt', startedAt,
            'firingCounter', firingCounter,
            'unresolvedCounter', unresolvedCounter,
            'firingStartTime', firingStartTime,
            'firingStartTimeSinceLastResolved', firingStartTimeSinceLastResolved
        ) || COALESCE(extra_data, '{}'::jsonb)
        WHERE event IS NULL 
          AND (service IS NOT NULL OR name IS NOT NULL)
        LIMIT 10000;

        GET DIAGNOSTICS rows_affected = ROW_COUNT;
        EXIT WHEN rows_affected = 0;
        COMMIT; 
    END LOOP;
END $$;

-- STEP 3: Drop the flat columns
ALTER TABLE alert 
DROP COLUMN IF EXISTS application, DROP COLUMN IF EXISTS object, DROP COLUMN IF EXISTS node_name, 
DROP COLUMN IF EXISTS severity, DROP COLUMN IF EXISTS message, DROP COLUMN IF EXISTS operator, 
DROP COLUMN IF EXISTS time_created, DROP COLUMN IF EXISTS network, DROP COLUMN IF EXISTS timezone, 
DROP COLUMN IF EXISTS custom_key, DROP COLUMN IF EXISTS expiry_in_minutes, DROP COLUMN IF EXISTS source, 
DROP COLUMN IF EXISTS service, DROP COLUMN IF EXISTS key_field, DROP COLUMN IF EXISTS name, 
DROP COLUMN IF EXISTS status, DROP COLUMN IF EXISTS description, DROP COLUMN IF EXISTS lastReceived, 
DROP COLUMN IF EXISTS isFullDuplicate, DROP COLUMN IF EXISTS isPartialDuplicate, 
DROP COLUMN IF EXISTS duplicateReason, DROP COLUMN IF EXISTS note, DROP COLUMN IF EXISTS assignee, 
DROP COLUMN IF EXISTS incident, DROP COLUMN IF EXISTS dismissUntil, DROP COLUMN IF EXISTS dismissed, 
DROP COLUMN IF EXISTS startedAt, DROP COLUMN IF EXISTS firingCounter, DROP COLUMN IF EXISTS unresolvedCounter, 
DROP COLUMN IF EXISTS firingStartTime, DROP COLUMN IF EXISTS firingStartTimeSinceLastResolved;
