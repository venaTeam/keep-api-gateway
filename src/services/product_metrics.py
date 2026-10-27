"""
Product BI metrics (Phase 2): user actions and alert status changes.

Counters live in src/repositories/metrics.py. This module owns the bounded
label allow-lists (the cardinality firewall) and the recording helpers used by
business-logic chokepoints (EnrichmentsBl.enrich_entity, incidents BL) and by
the POST /ui/events beacon.

Design:
- `tenant_id` comes from the authenticated identity.
- `source` (ui|api) comes from the X-Keep-Source request header, carried via a
  contextvar so deep chokepoints don't need the request object. keep-ui's
  ApiClient sets `X-Keep-Source: ui`; everything else defaults to `api`.
- `result` is success|error.
- All record_* helpers swallow their own errors: instrumentation must never
  break the request path.
"""

import contextvars
import logging

from src.models.action_type import ActionType
from src.repositories.metrics import (
    alert_status_change_total,
    ui_page_loads_total,
    user_action_total,
)

logger = logging.getLogger(__name__)

# --- bounded label allow-lists (cardinality firewall) ---
FEATURES = frozenset(
    {"alerts", "incidents", "presets", "dashboards", "providers", "workflows"}
)
ACTIONS = frozenset(
    {
        "change_status",
        "assign",
        "dismiss",
        "undismiss",
        "add_note",
        "delete_note",
        "comment",
        "mention",
        "create",
        "update",
        "delete",
        "enable",
        "disable",
    }
)
TO_STATUSES = frozenset({"firing", "acknowledged", "resolved", "suppressed"})
SOURCES = frozenset({"ui", "api"})
RESULTS = frozenset({"success", "error"})
# Bounded page-view route templates (mirrors keep-ui's PAGE_LABELS).
ROUTES = frozenset(
    {
        "incidents",
        "incidents_detail",
        "alerts_feed",
        "alerts_preset",
        "alerts_detail",
        "dashboard",
        "dashboard_detail",
    }
)

# Request source (ui|api), set per-request by middleware from X-Keep-Source.
_source_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "keep_metrics_source", default="api"
)


def set_request_source(value: str | None) -> None:
    """Set the source for the current request context (called by middleware)."""
    _source_ctx.set(value if value in SOURCES else "api")


def current_source() -> str:
    return _source_ctx.get()


def is_valid_action(feature: str, action: str) -> bool:
    return feature in FEATURES and action in ACTIONS


def record_user_action(
    *,
    tenant_id: str,
    feature: str,
    action: str,
    result: str = "success",
    source: str | None = None,
) -> bool:
    """
    Increment keep_user_action_total. Returns True if recorded, False if the
    labels failed the allow-list (so callers/tests can assert rejection).
    """
    try:
        if not is_valid_action(feature, action):
            logger.debug(
                "Rejected user-action metric (allow-list)",
                extra={"feature": feature, "action": action},
            )
            return False
        src = source if source in SOURCES else current_source()
        res = result if result in RESULTS else "success"
        user_action_total.labels(
            tenant_id=tenant_id,
            feature=feature,
            action=action,
            source=src,
            result=res,
        ).inc()
        return True
    except Exception:
        logger.debug("record_user_action failed", exc_info=True)
        return False


def record_page_load(*, tenant_id: str, route: str) -> bool:
    """Increment keep_ui_page_loads_total. Returns True if recorded."""
    try:
        if route not in ROUTES:
            return False
        ui_page_loads_total.labels(tenant_id=tenant_id, route=route).inc()
        return True
    except Exception:
        logger.debug("record_page_load failed", exc_info=True)
        return False


def record_alert_status_change(*, tenant_id: str, to_status: str) -> bool:
    """Increment keep_alert_status_change_total. Returns True if recorded."""
    try:
        if to_status not in TO_STATUSES:
            return False
        alert_status_change_total.labels(tenant_id=tenant_id, to_status=to_status).inc()
        return True
    except Exception:
        logger.debug("record_alert_status_change failed", exc_info=True)
        return False


# --- map the gateway's ActionType (+ enrichment body) to a product action ---
# Only user-facing alert actions map; system/rule enrichments return None and
# are not counted.
_ACTION_TYPE_TO_ACTION = {
    ActionType.DELETE_ALERT: "delete",
    ActionType.ACKNOWLEDGE: "assign",
    ActionType.COMMENT: "add_note",
    ActionType.UNCOMMENT: "delete_note",
    ActionType.MANUAL_STATUS_CHANGE: "change_status",
    ActionType.MANUAL_RESOLVE: "change_status",
    ActionType.API_STATUS_CHANGE: "change_status",
    ActionType.API_AUTOMATIC_RESOLVE: "change_status",
    ActionType.STATUS_UNENRICH: "undismiss",
}


def alert_action_for(action_type: ActionType, enrichments: dict) -> str | None:
    """
    Resolve the product `action` for an alert enrichment, or None to skip
    (system/rule enrichments, unknown types).

    Dismiss is a status change to `suppressed` carrying a dismiss_mode, so it is
    detected from the enrichment body rather than the (shared) action type.
    """
    if enrichments.get("dismiss_mode"):
        return "dismiss"
    return _ACTION_TYPE_TO_ACTION.get(action_type)


def record_alert_enrichment(
    *,
    tenant_id: str,
    action_type: ActionType,
    enrichments: dict,
    result: str = "success",
) -> None:
    """
    Chokepoint recorder for alert enrichments (called from
    EnrichmentsBl.enrich_entity). Emits keep_user_action_total for user actions
    and keep_alert_status_change_total when the enrichment sets a bounded status.
    Never raises.
    """
    try:
        action = alert_action_for(action_type, enrichments or {})
        if action is not None:
            record_user_action(
                tenant_id=tenant_id,
                feature="alerts",
                action=action,
                result=result,
            )
        # Status changes are only meaningful on success.
        if result == "success":
            status = (enrichments or {}).get("status")
            # A dismiss is a suppression even when the enrichment body only
            # carries dismiss_mode (no explicit status).
            if not status and action == "dismiss":
                status = "suppressed"
            if status in TO_STATUSES:
                record_alert_status_change(tenant_id=tenant_id, to_status=status)
    except Exception:
        logger.debug("record_alert_enrichment failed", exc_info=True)
