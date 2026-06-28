"""
Product BI metrics (Phase 3): point-in-time incident gauges.

Two gauges are recomputed periodically from the DB (counters can't answer
"how many right now"):

- keep_incident_alerts_associated{tenant_id}: alerts currently linked to
  incidents (active rows in lastalerttoincident).
- keep_incident_with_ticket{tenant_id, ticket_provider}: incidents currently
  linked to an external ticket, classified by provider (servicenow|jira|other).

Mirrors the active-users refresh pattern: a sync DB query run off the event
loop on an interval. Every worker computes the same value and sets a `livemax`
gauge, so the value is de-duplicated across processes. All helpers swallow their
own errors — instrumentation must never crash the service.
"""

import asyncio
import logging

from sqlmodel import Session, func, select

from src.models.db.alert import IncidentEnrichment, LastAlertToIncident
from src.models.db.helpers import NULL_FOR_DELETED_AT
from src.repositories import db as keep_db
from src.repositories.metrics import (incident_alerts_associated_gauge,
                                      incidents_with_ticket_gauge)

logger = logging.getLogger(__name__)

# Bounded ticket-provider classification (cardinality firewall).
TICKET_PROVIDERS = frozenset({"servicenow", "jira", "other"})


def _ticket_provider(enrichments: dict) -> str | None:
    """
    Classify an incident's external ticket by provider, or None if it has no
    ticket. Bounded to TICKET_PROVIDERS.
    """
    if not isinstance(enrichments, dict):
        return None
    has_ticket = bool(
        enrichments.get("ticket_url")
        or enrichments.get("ticket_id")
        or enrichments.get("ticket_type")
    )
    if not has_ticket:
        return None
    ttype = str(enrichments.get("ticket_type") or "").lower()
    turl = str(enrichments.get("ticket_url") or "").lower()
    if "servicenow" in ttype or "service-now" in turl or "service_now" in ttype:
        return "servicenow"
    if "jira" in ttype or "atlassian" in turl:
        return "jira"
    return "other"


def _count_alerts_in_incidents(session: Session) -> dict[str, int]:
    rows = session.exec(
        select(LastAlertToIncident.tenant_id, func.count())
        .where(LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT)
        .group_by(LastAlertToIncident.tenant_id)
    ).all()
    return {tenant_id: count for tenant_id, count in rows}


def _count_incidents_with_ticket(session: Session) -> dict[tuple[str, str], int]:
    """Count incidents with a ticket, keyed by (tenant_id, ticket_provider)."""
    rows = session.exec(
        select(IncidentEnrichment.tenant_id, IncidentEnrichment.enrichments)
    ).all()
    counts: dict[tuple[str, str], int] = {}
    for tenant_id, enrichments in rows:
        provider = _ticket_provider(enrichments)
        if provider is None:
            continue
        counts[(tenant_id, provider)] = counts.get((tenant_id, provider), 0) + 1
    return counts


# Label sets emitted on the previous refresh, so combos that drop to zero are
# explicitly set to 0 (a livemax gauge otherwise retains the last value).
_seen_alert_labels: set[str] = set()
_seen_ticket_labels: set[tuple[str, str]] = set()


def refresh_incident_metrics() -> None:
    """Recompute both incident gauges from the DB (synchronous)."""
    global _seen_alert_labels, _seen_ticket_labels
    with Session(keep_db.engine) as session:
        alerts_by_tenant = _count_alerts_in_incidents(session)
        tickets_by_tenant_provider = _count_incidents_with_ticket(session)

    # Alerts associated to incidents.
    for tenant_id in _seen_alert_labels - set(alerts_by_tenant):
        incident_alerts_associated_gauge.labels(tenant_id=tenant_id).set(0)
    for tenant_id, count in alerts_by_tenant.items():
        incident_alerts_associated_gauge.labels(tenant_id=tenant_id).set(count)
    _seen_alert_labels = set(alerts_by_tenant)

    # Incidents linked to an external ticket.
    for tenant_id, provider in _seen_ticket_labels - set(tickets_by_tenant_provider):
        incidents_with_ticket_gauge.labels(
            tenant_id=tenant_id, ticket_provider=provider
        ).set(0)
    for (tenant_id, provider), count in tickets_by_tenant_provider.items():
        incidents_with_ticket_gauge.labels(
            tenant_id=tenant_id, ticket_provider=provider
        ).set(count)
    _seen_ticket_labels = set(tickets_by_tenant_provider)


async def incident_metrics_refresh_loop(interval_seconds: int = 300) -> None:
    """Background loop: refresh the incident gauges every `interval_seconds`."""
    logger.info(
        "Starting incident-metrics refresh loop",
        extra={"interval_seconds": interval_seconds},
    )
    while True:
        try:
            # The query is synchronous; run it off the event loop.
            await asyncio.to_thread(refresh_incident_metrics)
        except Exception:
            logger.exception("Failed to refresh incident metrics")
        await asyncio.sleep(interval_seconds)
