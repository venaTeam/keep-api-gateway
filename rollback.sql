-- STEP 1: Restore the event and extra_data columns (if dropped)
ALTER TABLE alert ADD COLUMN IF NOT EXISTS event JSONB;
ALTER TABLE alert ADD COLUMN IF NOT EXISTS extra_data JSONB;

-- STEP 2: Re-populate event column from flat columns (Safe)
-- This moves the native columns back into the JSON 'event' blob
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
            'lastReceived', last_received,
            'isFullDuplicate', is_full_duplicate,
            'isPartialDuplicate', is_partial_duplicate,
            'duplicateReason', duplicate_reason,
            'note', note,
            'assignee', assignee,
            'incident', incident,
            'dismissUntil', dismiss_until,
            'dismissed', dismissed,
            'startedAt', started_at,
            'firingCounter', firing_counter,
            'unresolvedCounter', unresolved_counter,
            'firingStartTime', firing_start_time,
            'firingStartTimeSinceLastResolved', firing_start_time_since_last_resolved,
            'previous_status', previous_status,
            'maintenance_windows_trace', maintenance_windows_trace
        )
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
DROP COLUMN IF EXISTS firing_start_time_since_last_resolved,
DROP COLUMN IF EXISTS previous_status,
DROP COLUMN IF EXISTS maintenance_windows_trace;
