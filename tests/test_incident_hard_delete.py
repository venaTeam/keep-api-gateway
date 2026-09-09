"""Deleting an incident removes the incident and its own trail — nothing else.

`delete_incident_by_id` is a real DELETE now, not a status flip. What has to go:
the incident row, its alert links, its enrichment row, its audit trail and the
comment @mentions on that trail. What has to stay: the alerts themselves, their
LastAlert rows, their own audit history, and any link those alerts have to other
incidents.

The audit trail and the mentions are deleted explicitly rather than by cascade —
`alertaudit` rows for an incident are keyed by its UUID in the `fingerprint`
column with no FK, and `commentmention` lost its FK to `alertaudit` deliberately.
"""

import uuid
from datetime import datetime, timezone

from src.models.action_type import ActionType
from src.models.db.alert import (
    Alert,
    AlertAudit,
    CommentMention,
    IncidentEnrichment,
    LastAlert,
    LastAlertToIncident,
)
from src.models.db.incident import Incident
from src.repositories.db import delete_incident_by_id, enrich_entity
from src.repositories.dependencies import SINGLE_TENANT_UUID


def _make_incident(db_session, name="to-delete") -> Incident:
    incident = Incident(
        tenant_id=SINGLE_TENANT_UUID,
        user_generated_name=name,
        user_summary="s",
        generated_summary="s",
    )
    db_session.add(incident)
    db_session.commit()
    db_session.refresh(incident)
    return incident


def _make_alert(db_session, fingerprint: str) -> Alert:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    alert = Alert(
        id=uuid.uuid4(),
        tenant_id=SINGLE_TENANT_UUID,
        timestamp=now,
        provider_type="test",
        provider_id="test-1",
        fingerprint=fingerprint,
        name=f"alert {fingerprint}",
        severity="critical",
        status="firing",
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
        )
    )
    db_session.commit()
    return alert


def _link(db_session, incident, fingerprint):
    db_session.add(
        LastAlertToIncident(
            tenant_id=SINGLE_TENANT_UUID,
            incident_id=incident.id,
            fingerprint=fingerprint,
        )
    )
    db_session.commit()


def _audit_count(db_session, fingerprint) -> int:
    return (
        db_session.query(AlertAudit)
        .filter(
            AlertAudit.tenant_id == SINGLE_TENANT_UUID,
            AlertAudit.fingerprint == str(fingerprint),
        )
        .count()
    )


def test_delete_removes_the_incident_row(db_session):
    incident = _make_incident(db_session)
    assert delete_incident_by_id(SINGLE_TENANT_UUID, incident.id, db_session) is True
    assert db_session.get(Incident, incident.id) is None


def test_delete_reports_false_for_a_missing_incident(db_session):
    assert (
        delete_incident_by_id(SINGLE_TENANT_UUID, uuid.uuid4(), db_session) is False
    )


def test_delete_is_scoped_to_the_tenant(db_session):
    incident = _make_incident(db_session)
    assert delete_incident_by_id("some-other-tenant", incident.id, db_session) is False
    assert db_session.get(Incident, incident.id) is not None


def test_alerts_survive_and_only_the_link_goes(db_session):
    incident = _make_incident(db_session)
    alert = _make_alert(db_session, "del-keeps-alert")
    _link(db_session, incident, alert.fingerprint)

    delete_incident_by_id(SINGLE_TENANT_UUID, incident.id, db_session)
    db_session.expire_all()

    # The alert and its LastAlert pointer are untouched.
    # (Alert's primary key is composite — (id, timestamp) — so query, don't get.)
    assert db_session.query(Alert).filter(Alert.id == alert.id).count() == 1
    assert (
        db_session.query(LastAlert)
        .filter(LastAlert.fingerprint == "del-keeps-alert")
        .count()
        == 1
    )
    # ...and only the join row is gone.
    assert (
        db_session.query(LastAlertToIncident)
        .filter(LastAlertToIncident.incident_id == incident.id)
        .count()
        == 0
    )


def test_a_shared_alerts_other_incident_keeps_its_link(db_session):
    doomed = _make_incident(db_session, "doomed")
    keeper = _make_incident(db_session, "keeper")
    alert = _make_alert(db_session, "del-shared-alert")
    _link(db_session, doomed, alert.fingerprint)
    _link(db_session, keeper, alert.fingerprint)

    delete_incident_by_id(SINGLE_TENANT_UUID, doomed.id, db_session)
    db_session.expire_all()

    assert (
        db_session.query(LastAlertToIncident)
        .filter(LastAlertToIncident.incident_id == keeper.id)
        .count()
        == 1
    )
    assert db_session.get(Incident, keeper.id) is not None


def test_enrichment_row_is_removed(db_session):
    incident = _make_incident(db_session)
    enrich_entity(
        SINGLE_TENANT_UUID,
        str(incident.id),
        {"note": "dies with the incident"},
        ActionType.INCIDENT_ENRICH,
        "tester",
        "enriched",
        session=db_session,
        entity_type="incident",
    )
    assert (
        db_session.query(IncidentEnrichment)
        .filter(IncidentEnrichment.incident_id == incident.id)
        .count()
        == 1
    )

    delete_incident_by_id(SINGLE_TENANT_UUID, incident.id, db_session)
    db_session.expire_all()

    assert (
        db_session.query(IncidentEnrichment)
        .filter(IncidentEnrichment.incident_id == incident.id)
        .count()
        == 0
    )


def test_incident_audit_trail_is_removed(db_session):
    incident = _make_incident(db_session)
    enrich_entity(
        SINGLE_TENANT_UUID,
        str(incident.id),
        {"note": "audited"},
        ActionType.INCIDENT_ENRICH,
        "tester",
        "enriched",
        session=db_session,
        entity_type="incident",
    )
    assert _audit_count(db_session, incident.id) >= 1

    delete_incident_by_id(SINGLE_TENANT_UUID, incident.id, db_session)
    db_session.expire_all()

    assert _audit_count(db_session, incident.id) == 0


def test_alert_audit_history_is_not_collateral(db_session):
    """An alert's own audit trail must outlive the incident that grouped it."""
    incident = _make_incident(db_session)
    alert = _make_alert(db_session, "del-audit-kept")
    _link(db_session, incident, alert.fingerprint)
    enrich_entity(
        SINGLE_TENANT_UUID,
        alert.fingerprint,
        {"note": "alert note"},
        ActionType.GENERIC_ENRICH,
        "tester",
        "enriched alert",
        session=db_session,
        entity_type="alert",
    )
    before = _audit_count(db_session, alert.fingerprint)
    assert before >= 1

    delete_incident_by_id(SINGLE_TENANT_UUID, incident.id, db_session)
    db_session.expire_all()

    assert _audit_count(db_session, alert.fingerprint) == before


def test_comment_mentions_on_the_trail_are_removed(db_session):
    incident = _make_incident(db_session)
    audit = AlertAudit(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint=str(incident.id),
        user_id="tester",
        action=ActionType.INCIDENT_COMMENT.value,
        description="hey @someone",
    )
    db_session.add(audit)
    db_session.commit()
    db_session.add(
        CommentMention(
            tenant_id=SINGLE_TENANT_UUID,
            comment_id=audit.id,
            mentioned_user_id="someone",
        )
    )
    db_session.commit()
    assert (
        db_session.query(CommentMention)
        .filter(CommentMention.comment_id == audit.id)
        .count()
        == 1
    )

    delete_incident_by_id(SINGLE_TENANT_UUID, incident.id, db_session)
    db_session.expire_all()

    # No FK to cascade through, so this only passes if it is deleted explicitly.
    assert (
        db_session.query(CommentMention)
        .filter(CommentMention.comment_id == audit.id)
        .count()
        == 0
    )


def test_another_incidents_audit_trail_is_untouched(db_session):
    doomed = _make_incident(db_session, "doomed-audit")
    keeper = _make_incident(db_session, "keeper-audit")
    for incident in (doomed, keeper):
        enrich_entity(
            SINGLE_TENANT_UUID,
            str(incident.id),
            {"note": "n"},
            ActionType.INCIDENT_ENRICH,
            "tester",
            "enriched",
            session=db_session,
            entity_type="incident",
        )
    keeper_before = _audit_count(db_session, keeper.id)

    delete_incident_by_id(SINGLE_TENANT_UUID, doomed.id, db_session)
    db_session.expire_all()

    assert _audit_count(db_session, doomed.id) == 0
    assert _audit_count(db_session, keeper.id) == keeper_before
