"""Confirming a candidate incident (from a "require approval" rule, or an AI
prediction) turns it into a real incident, and only then do the alerts already
grouped into it take its state.
"""

import pytest
from sqlmodel import select

from src.models.db.alert import LastAlert
from src.models.db.incident import Incident, IncidentStatus
from tests.fixtures.alert_graph import link, seed_alert, seed_incident
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

AUTH = {"x-api-key": "some-key"}


def _candidate(db_session, status=IncidentStatus.FIRING, assignee=None):
    incident = seed_incident(db_session, "candidate", status, is_candidate=True)
    incident.assignee = assignee
    db_session.add(incident)
    db_session.commit()
    return incident


def _last_alert(db_session, fingerprint):
    db_session.expire_all()
    return db_session.exec(
        select(LastAlert).where(LastAlert.fingerprint == fingerprint)
    ).one()


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_confirming_makes_the_candidate_a_real_incident(db_session, client, test_app):
    incident = _candidate(db_session)

    response = client.post(f"/incidents/{incident.id}/confirm", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["is_candidate"] is False
    db_session.expire_all()
    stored = db_session.get(Incident, incident.id)
    assert stored.is_candidate is False
    assert stored.is_visible is True


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_confirming_hands_the_incident_state_to_its_alerts(db_session, client, test_app):
    incident = _candidate(
        db_session, IncidentStatus.ACKNOWLEDGED, assignee="rule-owner@keep"
    )
    seed_alert(db_session, "fp-grouped")
    link(db_session, "fp-grouped", incident)
    assert _last_alert(db_session, "fp-grouped").assignee is None

    response = client.post(f"/incidents/{incident.id}/confirm", headers=AUTH)

    assert response.status_code == 200
    last_alert = _last_alert(db_session, "fp-grouped")
    assert last_alert.assignee == "rule-owner@keep"
    assert last_alert.status == "acknowledged"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_only_a_candidate_can_be_confirmed(db_session, client, test_app):
    incident = seed_incident(db_session, "already real")

    response = client.post(f"/incidents/{incident.id}/confirm", headers=AUTH)

    assert response.status_code == 404
