"""Which `status` a caller may send when creating or updating an incident.

`status` is not a declared field on IncidentDtoIn, so it used to be stored as
whatever was sent: 'deleted' was saved, read back as firing, and showed up under
its own 'deleted' facet. Firing, acknowledged and resolved are accepted on both
routes; suppressed and merged have their own flows, and anything else is not a
status at all.
"""

import pytest
from sqlmodel import select

from src.models.action_type import ActionType
from src.models.db.alert import AlertAudit, LastAlert
from src.models.db.incident import Incident, IncidentDismissMode
from tests.fixtures.alert_graph import link, seed_alert, seed_incident
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

AUTH = {"x-api-key": "some-key"}

REJECTED = ["deleted", "suppressed", "merged", "not-a-status", None]
ACCEPTED = ["firing", "acknowledged", "resolved"]


def _create(client, **body):
    return client.post(
        "/incidents",
        headers=AUTH,
        json={"user_generated_name": "status input", "user_summary": "s", **body},
    )


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("status", REJECTED)
def test_create_rejects_a_status_that_cannot_be_set(
    db_session, client, test_app, status
):
    response = _create(client, status=status)

    assert response.status_code == 422
    assert "must be one of firing, acknowledged, resolved" in response.text
    assert db_session.exec(select(Incident)).all() == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("status", ACCEPTED)
def test_create_stores_a_settable_status(db_session, client, test_app, status):
    response = _create(client, status=status.upper())

    assert response.status_code == 202
    assert response.json()["status"] == status
    stored = db_session.exec(select(Incident)).one()
    assert stored.status == status


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_create_without_a_status_is_firing(db_session, client, test_app):
    response = _create(client)

    assert response.status_code == 202
    assert db_session.exec(select(Incident)).one().status == "firing"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("status", REJECTED)
def test_update_rejects_a_status_that_cannot_be_set(
    db_session, client, test_app, status
):
    incident_id = _create(client).json()["id"]

    response = client.put(
        f"/incidents/{incident_id}",
        headers=AUTH,
        json={"user_generated_name": "status input", "status": status},
    )

    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.exec(select(Incident)).one().status == "firing"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("status", ["acknowledged", "resolved"])
def test_update_still_sets_a_settable_status(db_session, client, test_app, status):
    """QA sets status through the update route; it keeps working."""
    incident_id = _create(client).json()["id"]

    response = client.put(
        f"/incidents/{incident_id}",
        headers=AUTH,
        json={"user_generated_name": "status input", "status": status},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.exec(select(Incident)).one().status == status


# === a status sent through PUT is a real status change ===


def _put_status(client, incident_id, status):
    return client.put(
        f"/incidents/{incident_id}",
        headers=AUTH,
        json={"user_generated_name": "status input", "status": status},
    )


def _status_audits(db_session, incident_id):
    return db_session.exec(
        select(AlertAudit).where(
            AlertAudit.fingerprint == str(incident_id),
            AlertAudit.action == ActionType.INCIDENT_STATUS_CHANGE.value,
        )
    ).all()


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_to_firing_lifts_a_dismissal(db_session, client, test_app):
    incident = seed_incident(db_session, "dismissed")
    incident.dismiss_mode = IncidentDismissMode.PERMANENT.value
    db_session.add(incident)
    db_session.commit()

    response = _put_status(client, incident.id, "firing")

    assert response.status_code == 200
    assert response.json()["status"] == "firing"
    db_session.expire_all()
    stored = db_session.get(Incident, incident.id)
    assert stored.dismiss_mode is None
    assert stored.get_effective_status() == "firing"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_to_acknowledged_reaches_the_alerts(db_session, client, test_app):
    incident = seed_incident(db_session, "ack via put")
    seed_alert(db_session, "fp-put-ack")
    link(db_session, "fp-put-ack", incident)

    response = _put_status(client, incident.id, "acknowledged")

    assert response.status_code == 200
    db_session.expire_all()
    last_alert = db_session.exec(
        select(LastAlert).where(LastAlert.fingerprint == "fp-put-ack")
    ).one()
    assert last_alert.status == "acknowledged"
    # A status change assigns no one, whichever route it comes through.
    assert last_alert.assignee is None
    assert db_session.get(Incident, incident.id).assignee is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_to_resolved_sets_end_time_and_audits(db_session, client, test_app):
    incident = seed_incident(db_session, "resolve via put")

    response = _put_status(client, incident.id, "resolved")

    assert response.status_code == 200
    db_session.expire_all()
    stored = db_session.get(Incident, incident.id)
    assert stored.status == "resolved"
    assert stored.end_time is not None
    assert len(_status_audits(db_session, incident.id)) == 1


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_to_the_current_status_changes_nothing(db_session, client, test_app):
    incident = seed_incident(db_session, "already firing")

    response = _put_status(client, incident.id, "firing")

    assert response.status_code == 200
    assert _status_audits(db_session, incident.id) == []
