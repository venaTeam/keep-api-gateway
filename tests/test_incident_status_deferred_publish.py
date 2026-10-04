from datetime import datetime, timezone
from unittest.mock import AsyncMock
import uuid
import pytest

from src.models.alert import AlertStatus
from src.models.action_type import ActionType
from src.models.db.alert import Alert, LastAlert, LastAlertToIncident
from src.models.db.incident import Incident, IncidentDismissMode, IncidentStatus
from src.models.incident import IncidentStatusChangeDto
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.enrichments_bl import EnrichmentsBl
from src.services.incidents_bl import IncidentBl
from src.services.producers.base_event_handler import EventProducer, EventType

ACTOR = AuthenticatedEntity(tenant_id=SINGLE_TENANT_UUID, email="tester@keep")


def _incident(db_session, status=IncidentStatus.FIRING, **kwargs) -> Incident:
    incident = Incident(
        tenant_id=SINGLE_TENANT_UUID,
        user_generated_name="prop",
        user_summary="s",
        generated_summary="s",
        status=status.value,
        **kwargs,
    )
    db_session.add(incident)
    db_session.commit()
    db_session.refresh(incident)
    return incident


def _alert(db_session, fingerprint, provider_status="firing", **last_alert_kwargs):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    alert = Alert(
        id=uuid.uuid4(),
        tenant_id=SINGLE_TENANT_UUID,
        timestamp=now,
        provider_type="test",
        provider_id="t1",
        fingerprint=fingerprint,
        name=fingerprint,
        severity="critical",
        status=provider_status,
    )
    db_session.add(alert)
    db_session.flush()
    db_session.add(
        LastAlert(
            tenant_id=SINGLE_TENANT_UUID,
            fingerprint=fingerprint,
            alert_id=alert.id,
            timestamp=now,
            first_timestamp=now,
            last_received=now,
            **last_alert_kwargs,
        )
    )
    db_session.commit()
    return fingerprint


def _link(db_session, incident, fingerprint):
    db_session.add(
        LastAlertToIncident(
            tenant_id=SINGLE_TENANT_UUID,
            incident_id=incident.id,
            fingerprint=fingerprint,
        )
    )
    db_session.commit()


@pytest.mark.asyncio
async def test_change_status_publishes_propagated_alerts_after_commit(db_session):
    incident = _incident(db_session, status=IncidentStatus.FIRING)
    fp1 = _alert(db_session, "alert-1")
    fp2 = _alert(db_session, "alert-2")
    _link(db_session, incident, fp1)
    _link(db_session, incident, fp2)

    mock_producer = AsyncMock(spec=EventProducer)
    enrichment_bl = EnrichmentsBl(SINGLE_TENANT_UUID, db=db_session, event_producer=mock_producer)
    incident_bl = IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)

    await incident_bl.change_status(
        incident_id=incident.id,
        new_status=IncidentStatus.ACKNOWLEDGED,
        change_by=ACTOR,
        commit=True,
        enrichment_bl=enrichment_bl,
    )

    # Verify event producer produce was called for the batch of alerts
    mock_producer.produce.assert_called_once()
    kwargs = mock_producer.produce.call_args.kwargs

    assert kwargs["event_type"] == EventType.BATCH_ENRICH
    assert sorted(kwargs["fingerprint"]) == sorted([fp1, fp2])
    assert kwargs["event"]["status"] == "acknowledged"
    assert kwargs["event"]["assignee"] == ACTOR.email


@pytest.mark.asyncio
async def test_enrich_and_change_status_publishes_both_incident_and_alerts(db_session):
    incident = _incident(db_session, status=IncidentStatus.FIRING)
    fp1 = _alert(db_session, "alert-enrich-1")
    _link(db_session, incident, fp1)

    mock_producer = AsyncMock(spec=EventProducer)
    enrichment_bl = EnrichmentsBl(SINGLE_TENANT_UUID, db=db_session, event_producer=mock_producer)
    incident_bl = IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)

    change = IncidentStatusChangeDto(status=IncidentStatus.ACKNOWLEDGED)
    enrichments = {"custom_incident_note": "Investigating incident"}

    await incident_bl.enrich_and_change_status(
        incident_id=incident.id,
        change=change,
        enrichments=enrichments,
        change_by=ACTOR,
        enrichment_bl=enrichment_bl,
    )

    # There should be TWO published events:
    # 1. Incident enrichment event (single incident)
    # 2. Alert batch enrich event (propagated alerts)
    assert mock_producer.produce.call_count == 2

    calls = mock_producer.produce.call_args_list

    # Call 1: incident enrichment
    call1_kwargs = calls[0].kwargs
    assert call1_kwargs["event_type"] == EventType.ENRICH
    assert call1_kwargs["fingerprint"] == incident.id
    assert call1_kwargs["event"]["custom_incident_note"] == "Investigating incident"
    assert call1_kwargs["event"]["action_type"] == ActionType.INCIDENT_ENRICH.value

    # Call 2: alert batch enrichment
    call2_kwargs = calls[1].kwargs
    assert call2_kwargs["event_type"] == EventType.BATCH_ENRICH
    assert call2_kwargs["fingerprint"] == [fp1]
    assert call2_kwargs["event"]["status"] == "acknowledged"
    assert call2_kwargs["event"]["assignee"] == ACTOR.email


@pytest.mark.asyncio
async def test_change_status_does_not_publish_when_commit_is_false(db_session):
    incident = _incident(db_session, status=IncidentStatus.FIRING)
    fp1 = _alert(db_session, "alert-defer-1")
    _link(db_session, incident, fp1)

    mock_producer = AsyncMock(spec=EventProducer)
    enrichment_bl = EnrichmentsBl(SINGLE_TENANT_UUID, db=db_session, event_producer=mock_producer)
    incident_bl = IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)

    await incident_bl.change_status(
        incident_id=incident.id,
        new_status=IncidentStatus.ACKNOWLEDGED,
        change_by=ACTOR,
        commit=False,
        enrichment_bl=enrichment_bl,
    )

    # Before commit/flush, nothing should be published
    mock_producer.produce.assert_not_called()

    # After the caller commits and flushes:
    db_session.commit()
    await incident_bl._flush_deferred_publishes()

    mock_producer.produce.assert_called_once()
    assert mock_producer.produce.call_args.kwargs["event_type"] == EventType.BATCH_ENRICH
