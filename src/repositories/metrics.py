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

# Total incidents opened. `source` is bounded to how the incident was created
# (manual|rule|ai) — rule_name was dropped from the label set as it is
# unbounded (cardinality firewall). Use increase(...[$__range]) for "opened in
# the selected interval".
incidents_opened_total = Counter(
    f"{INCIDENT_METRIC_PREFIX}opened_total",
    "Total number of incidents opened",
    labelnames=["tenant_id", "source"],
)

# Point-in-time count of alerts currently associated to incidents, refreshed
# periodically from the DB (src/services/incident_metrics.py). Every worker
# computes the same DB-derived value, so `livemax` de-duplicates it across
# processes (rather than summing identical values like livesum would); livemax
# (not max) drops values from dead workers on restart.
incident_alerts_associated_gauge = Gauge(
    f"{INCIDENT_METRIC_PREFIX}alerts_associated",
    "Number of alerts currently associated to incidents",
    labelnames=["tenant_id"],
    multiprocess_mode="livemax",
)

# Point-in-time count of incidents linked to an external ticket, by provider
# (e.g. servicenow). Refreshed from incident enrichments. Same livemax rationale
# as above.
incidents_with_ticket_gauge = Gauge(
    f"{INCIDENT_METRIC_PREFIX}with_ticket",
    "Number of incidents currently linked to an external ticket, by provider",
    labelnames=["tenant_id", "ticket_provider"],
    multiprocess_mode="livemax",
)

### AUTH / USERS
# Failed login attempts. `reason` is bounded (empty_password|invalid_credentials).
# Incremented on the gateway /signin 401 path (DB auth). noauth never fails and
# external IdP (auth0/keycloak/okta/...) failures happen off-box and are not
# captured here.
login_failures_total = Counter(
    f"{METRIC_PREFIX}login_failures_total",
    "Total number of failed login attempts (HTTP 401 on /signin)",
    labelnames=["reason"],
)

### MAINTENANCE
MAINTENANCE_METRIC_PREFIX = "keep_maintenance_"

alerts_maintenance_silenced_total = Counter(
    f"{MAINTENANCE_METRIC_PREFIX}silenced_total",
    "Total number of alerts silenced by maintenance window",
    labelnames=["tenant_id", "maintenance_window_id", "maintenance_window_name"],
)
