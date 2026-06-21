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

### PRODUCT BI — USER ACTIONS (Phase 2)
# Product action counter. Labels are bounded by a server-side allow-list
# (src/services/product_metrics.py): tenant_id, feature, action, source (ui|api),
# result (success|error). Supersedes the keep-ui keep_ui_action_executions_total.
user_action_total = Counter(
    f"{METRIC_PREFIX}user_action_total",
    "Product actions by feature/action, with UI-vs-API origin and result",
    labelnames=["tenant_id", "feature", "action", "source", "result"],
)

# Dedicated alert status-change counter (resulting-status distribution is a
# headline question). to_status is bounded to the alert status enum.
alert_status_change_total = Counter(
    f"{METRIC_PREFIX}alert_status_change_total",
    "Alert status changes by resulting status",
    labelnames=["tenant_id", "to_status"],
)

# Page views, moved server-side from the keep-ui in-memory counter. Fed by the
# keep-ui beacon (POST /ui/page-view); route is a bounded Next.js route template.
ui_page_loads_total = Counter(
    f"{METRIC_PREFIX}ui_page_loads_total",
    "keep-ui page views by route",
    labelnames=["tenant_id", "route"],
)

### MAINTENANCE
MAINTENANCE_METRIC_PREFIX = "keep_maintenance_"

alerts_maintenance_silenced_total = Counter(
    f"{MAINTENANCE_METRIC_PREFIX}silenced_total",
    "Total number of alerts silenced by maintenance window",
    labelnames=["tenant_id", "maintenance_window_id", "maintenance_window_name"],
)
