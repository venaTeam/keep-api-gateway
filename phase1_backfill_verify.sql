-- Phase 1 / task 1.5 — backfill integrity verification.
-- Run AFTER `alembic upgrade head` (revs c1d2e3f4a5b6 .. c4d5e6f7a8b9) against the
-- target DB. Phase 1 is NOT done until every check below passes.
-- Read-only. Safe on a live database.

\echo '== 1. Row counts =='
-- Every lastalert points to exactly one alert; both numbers should match.
SELECT
    (SELECT count(*) FROM lastalert) AS lastalert_rows,
    (SELECT count(*) FROM lastalert la JOIN alert a ON la.alert_id = a.id) AS joined_rows;

\echo '== 2. Tracking-field backfill mismatches (expect 0 each) =='
-- lastalert tracking columns must equal the pointed-to alert row (task 1.3).
SELECT
    count(*) FILTER (WHERE la.last_received IS DISTINCT FROM a.last_received)                                   AS bad_last_received,
    count(*) FILTER (WHERE la.firing_counter IS DISTINCT FROM COALESCE(a.firing_counter, 0))                    AS bad_firing_counter,
    count(*) FILTER (WHERE la.unresolved_counter IS DISTINCT FROM COALESCE(a.unresolved_counter, 0))            AS bad_unresolved_counter,
    count(*) FILTER (WHERE la.started_at IS DISTINCT FROM a.started_at)                                         AS bad_started_at,
    count(*) FILTER (WHERE la.firing_start_time IS DISTINCT FROM a.firing_start_time)                           AS bad_firing_start_time,
    count(*) FILTER (WHERE la.firing_start_time_since_last_resolved
                           IS DISTINCT FROM a.firing_start_time_since_last_resolved)                            AS bad_fst_since_resolved
FROM lastalert la
JOIN alert a ON la.alert_id = a.id;

\echo '== 3. Enrichment override coverage =='
-- How many alertenrichment rows map to a lastalert, and how many carry each key.
SELECT
    count(*)                                                                       AS enrichment_rows,
    count(*) FILTER (WHERE jsonb_exists(ae.enrichments, 'status'))                 AS with_status,
    count(*) FILTER (WHERE jsonb_exists(ae.enrichments, 'assignee'))               AS with_assignee,
    count(*) FILTER (WHERE jsonb_exists(ae.enrichments, 'dismissUntil'))           AS with_dismiss_until,
    count(*) FILTER (WHERE COALESCE(ae.enrichments->>'dismissed','') = 'true')     AS with_dismissed_true
FROM alertenrichment ae
JOIN lastalert la ON la.tenant_id = ae.tenant_id AND la.fingerprint = ae.alert_fingerprint;

\echo '== 4. Status override mismatches (expect 0) =='
-- Where alertenrichment has an explicit status, lastalert.status must match it (task 1.4).
SELECT count(*) AS bad_status_override
FROM alertenrichment ae
JOIN lastalert la ON la.tenant_id = ae.tenant_id AND la.fingerprint = ae.alert_fingerprint
WHERE jsonb_exists(ae.enrichments, 'status')
  AND la.status IS DISTINCT FROM ae.enrichments->>'status';

\echo '== 5. Dismiss translation mismatches (expect 0) =='
-- dismissed:true with no explicit status -> status must be suppressed + a dismiss_mode set.
SELECT count(*) AS bad_dismiss_translation
FROM alertenrichment ae
JOIN lastalert la ON la.tenant_id = ae.tenant_id AND la.fingerprint = ae.alert_fingerprint
WHERE (COALESCE(ae.enrichments->>'dismissed','') = 'true'
       OR (jsonb_exists(ae.enrichments,'dismissUntil') AND COALESCE(ae.enrichments->>'dismissUntil','') <> ''))
  AND NOT jsonb_exists(ae.enrichments, 'status')
  AND (la.status IS DISTINCT FROM 'suppressed' OR la.dismiss_mode IS NULL);

\echo '== 6. Spot-check 50 random backfilled fingerprints =='
SELECT la.tenant_id, la.fingerprint,
       la.status, la.dismiss_mode, la.dismissed_until, la.assignee,
       la.last_received, la.firing_counter, la.unresolved_counter,
       ae.enrichments
FROM lastalert la
LEFT JOIN alertenrichment ae
       ON ae.tenant_id = la.tenant_id AND ae.alert_fingerprint = la.fingerprint
ORDER BY random()
LIMIT 50;
