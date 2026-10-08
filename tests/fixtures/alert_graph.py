from datetime import datetime, timedelta

from src.models.db.alert import (
    Alert,
    IncidentEnrichment,
    LastAlert,
    LastAlertToIncident,
)
from src.models.db.incident import Incident, IncidentStatus
from src.repositories.dependencies import SINGLE_TENANT_UUID


def seed_alert(
    session,
    fingerprint,
    *,
    name="alert",
    service=None,
    node_name=None,
    application=None,
    site=None,
    assignee=None,
    status="firing",
    severity="critical",
    tenant_id=SINGLE_TENANT_UUID,
    minutes_ago=1,
):
    timestamp = datetime.utcnow() - timedelta(minutes=minutes_ago)
    alert = Alert(
        tenant_id=tenant_id,
        provider_type="test",
        provider_id="test-provider",
        fingerprint=fingerprint,
        name=name,
        service=service,
        node_name=node_name,
        application=application,
        site=site,
        status=status,
        severity=severity,
        timestamp=timestamp,
    )
    session.add(alert)
    session.commit()
    session.add(
        LastAlert(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            alert_id=alert.id,
            timestamp=timestamp,
            first_timestamp=timestamp,
            assignee=assignee,
        )
    )
    session.commit()
    return alert


def seed_incident(
    session,
    name,
    status=IncidentStatus.FIRING,
    tenant_id=SINGLE_TENANT_UUID,
    enrichment_status=None,
    *,
    is_visible=True,
    is_candidate=False,
):
    incident = Incident(
        tenant_id=tenant_id,
        user_generated_name=name,
        user_summary=name,
        generated_summary=name,
        status=status.value,
        is_visible=is_visible,
        is_candidate=is_candidate,
    )
    session.add(incident)
    session.commit()
    if enrichment_status is not None:
        session.add(
            IncidentEnrichment(
                tenant_id=tenant_id,
                incident_id=incident.id,
                enrichments={"status": enrichment_status.value},
            )
        )
        session.commit()
    return incident


def link(
    session, fingerprint, incident, *, tenant_id=SINGLE_TENANT_UUID, deleted_at=None
):
    kwargs = {} if deleted_at is None else {"deleted_at": deleted_at}
    session.add(
        LastAlertToIncident(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            incident_id=incident.id,
            **kwargs,
        )
    )
    session.commit()
