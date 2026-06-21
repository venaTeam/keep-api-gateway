import os
from prometheus_client import Counter, Gauge, Histogram, Summary

# This MUST be called before any prometheus_client import
prom_multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus")
os.environ["PROMETHEUS_MULTIPROC_DIR"] = prom_multiproc_dir
try:
    os.makedirs(prom_multiproc_dir, exist_ok=True)
except Exception:
    # This might fail if we don't have permissions, but we shouldn't crash
    pass


def init_metrics():
    # Deprecated: logic moved to top level
    pass


# Initialize metrics configuration
init_metrics()

METRIC_PREFIX = "keep_"

# Process event metrics
events_in_counter = Counter(
    f"{METRIC_PREFIX}events_in_total",
    "Total number of events received",
)
events_out_counter = Counter(
    f"{METRIC_PREFIX}events_processed_total",
    "Total number of events processed",
)
events_error_counter = Counter(
    f"{METRIC_PREFIX}events_error_total",
    "Total number of events with error",
)
processing_time_summary = Summary(
    f"{METRIC_PREFIX}processing_time_seconds",
    "Average time spent processing events",
)

running_tasks_gauge = Gauge(
    f"{METRIC_PREFIX}running_tasks_current",
    "Current number of running tasks",
    multiprocess_mode="livesum",
)

running_tasks_by_process_gauge = Gauge(
    f"{METRIC_PREFIX}running_tasks_by_process",
    "Current number of running tasks per process",
    labelnames=["pid"],
    multiprocess_mode="livesum",
)

### ALERTS
ALERT_METRIC_PREFIX = "keep_alert_"

alert_ingestion_total = Counter(
    f"{ALERT_METRIC_PREFIX}ingestion_total",
    "Total number of alerts received",
    labelnames=["source", "status"],
)

alert_ingestion_error_total = Counter(
    f"{ALERT_METRIC_PREFIX}ingestion_error_total",
    "Total number of alerts received with error",
    labelnames=["source", "error_type"],
)

alert_enrichment_duration_seconds = Histogram(
    f"{ALERT_METRIC_PREFIX}enrichment_duration_seconds",
    "Time spent enriching alerts",
    labelnames=["source"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
)

deduplication_events_total = Counter(
    f"{ALERT_METRIC_PREFIX}deduplication_events_total",
    "Total number of deduplicated events",
    labelnames=["provider_type", "status"],
)

deduplication_duration_seconds = Histogram(
    f"{ALERT_METRIC_PREFIX}deduplication_duration_seconds",
    "Time spent deduplicating events",
    labelnames=["provider_type"],
)

rules_engine_duration_seconds = Histogram(
    f"{ALERT_METRIC_PREFIX}rules_engine_duration_seconds",
    "Time spent in rules engine",
    labelnames=["provider_type"],
)

### INCIDENTS
INCIDENT_METRIC_PREFIX = "keep_incident_"

incidents_opened_total = Counter(
    f"{INCIDENT_METRIC_PREFIX}opened_total",
    "Total number of incidents opened",
    labelnames=["tenant_id", "rule_id", "rule_name"],
)

### ACTIVE & CONNECTED USERS (Product BI — Phase 1)
# Replaces the keep-ui in-memory `keep_ui_active_users` gauge, which was driven
# by a random localStorage UUID, had no tenant dimension and was replica-unsafe.
# Both gauges below are keyed by the real authenticated tenant.

# Live/concurrent users: incremented on SSE subscribe, decremented on disconnect.
# Connections are partitioned across workers/replicas, so `livesum` aggregates
# the per-process counts into the true total.
connected_users_gauge = Gauge(
    f"{METRIC_PREFIX}connected_users",
    "Currently connected live users (authenticated SSE subscriptions)",
    labelnames=["tenant_id"],
    multiprocess_mode="livesum",
)

# DAU/WAU/MAU: distinct users with an audited action in a rolling window,
# recomputed periodically from AlertAudit. Every worker computes the same
# DB-derived value, so taking the max de-duplicates it across processes
# (rather than summing identical values like livesum would). `livemax` (not
# `max`) so values from dead workers are not retained across restarts — relies
# on mark_process_dead() being called on worker exit.
active_users_gauge = Gauge(
    f"{METRIC_PREFIX}active_users",
    "Distinct users with an audited action within the rolling window (DAU/WAU/MAU)",
    labelnames=["tenant_id", "window"],
    multiprocess_mode="livemax",
)

### MAINTENANCE
MAINTENANCE_METRIC_PREFIX = "keep_maintenance_"

alerts_maintenance_silenced_total = Counter(
    f"{MAINTENANCE_METRIC_PREFIX}silenced_total",
    "Total number of alerts silenced by maintenance window",
    labelnames=["tenant_id", "maintenance_window_id", "maintenance_window_name"],
)
