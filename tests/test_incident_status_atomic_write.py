"""An incident status change and the alert writes it causes land in one commit.

`change_status(commit=False)` and `enrich_and_change_status` stage the incident
row, its enrichment and the propagated alert columns on one session, so either
all of it is durable or none of it is.
"""

from datetime import datetime, timezone
import uuid
import pytest

from src.models.db.alert import Alert, IncidentEnrichment, LastAlert, LastAlertToIncident
from src.models.db.incident import Incident, IncidentStatus
from src.models.incident import IncidentStatusChangeDto
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.enrichments_bl import EnrichmentsBl
from src.services.incidents_bl import IncidentBl
from sqlalchemy.orm import Session as SASession
from sqlmodel import select

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


def _last_alert(db_session, fingerprint) -> LastAlert:
    db_session.expire_all()
    return db_session.exec(
        select(LastAlert).where(
            LastAlert.tenant_id == SINGLE_TENANT_UUID,
            LastAlert.fingerprint == fingerprint,
        )
    ).one()


@pytest.mark.asyncio
async def test_change_status_without_commit_commits_nothing(db_session, monkeypatch):
    # Asserted by spying on commit rather than by rolling back: the SQLite
    # fixture runs in autocommit, so a rollback there undoes nothing.
    incident = _incident(db_session, status=IncidentStatus.FIRING)
    fp = _alert(db_session, "alert-defer-1")
    _link(db_session, incident, fp)

    commits = []
    original_commit = SASession.commit

    def spy(self, *args, **kwargs):
        commits.append(self)
        return original_commit(self, *args, **kwargs)

    monkeypatch.setattr(SASession, "commit", spy)

    incident_bl = IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)
    await incident_bl.change_status(
        incident_id=incident.id,
        new_status=IncidentStatus.ACKNOWLEDGED,
        change_by=ACTOR,
        commit=False,
        enrichment_bl=EnrichmentsBl(SINGLE_TENANT_UUID, db=db_session),
    )

    assert commits == []


@pytest.mark.asyncio
async def test_change_status_without_commit_lands_on_the_callers_commit(db_session):
    incident = _incident(db_session, status=IncidentStatus.FIRING)
    fp = _alert(db_session, "alert-defer-2")
    _link(db_session, incident, fp)

    incident_bl = IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)
    await incident_bl.change_status(
        incident_id=incident.id,
        new_status=IncidentStatus.ACKNOWLEDGED,
        change_by=ACTOR,
        commit=False,
        enrichment_bl=EnrichmentsBl(SINGLE_TENANT_UUID, db=db_session),
    )

    db_session.commit()

    last_alert = _last_alert(db_session, fp)
    assert last_alert.status == "acknowledged"
    assert last_alert.assignee == ACTOR.email


@pytest.mark.asyncio
async def test_enrich_and_change_status_writes_incident_and_alerts(db_session):
    incident = _incident(db_session, status=IncidentStatus.FIRING)
    fp = _alert(db_session, "alert-enrich-1")
    _link(db_session, incident, fp)

    incident_bl = IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)
    result = await incident_bl.enrich_and_change_status(
        incident_id=incident.id,
        change=IncidentStatusChangeDto(status=IncidentStatus.ACKNOWLEDGED),
        enrichments={"custom_incident_note": "Investigating incident"},
        change_by=ACTOR,
        enrichment_bl=EnrichmentsBl(SINGLE_TENANT_UUID, db=db_session),
    )

    assert result.status == IncidentStatus.ACKNOWLEDGED

    db_session.expire_all()
    enrichment = db_session.exec(
        select(IncidentEnrichment).where(
            IncidentEnrichment.tenant_id == SINGLE_TENANT_UUID,
            IncidentEnrichment.incident_id == incident.id,
        )
    ).one()
    assert enrichment.enrichments["custom_incident_note"] == "Investigating incident"

    last_alert = _last_alert(db_session, fp)
    assert last_alert.status == "acknowledged"
    assert last_alert.assignee == ACTOR.email
