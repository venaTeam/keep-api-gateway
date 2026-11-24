"""Querying incidents by their dismissal.

The incidents list's default view hides suppressed incidents, so it can't see
when a time-boxed dismissal is about to lapse. The UI asks for the earliest live
`dismissed_until` on its own with exactly this query.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.db.incident import IncidentDismissMode
from tests.fixtures.alert_graph import seed_incident
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

AUTH = {"x-api-key": "some-key"}

NEXT_EXPIRY_QUERY = {
    "candidate": "false",
    "limit": 1,
    "sorting": "dismissed_until",
    "cel": "status == 'suppressed' && dismiss_mode == 'dismiss_until'",
}


@pytest.fixture(autouse=True)
def incidents_engine(db_session, monkeypatch):
    # The incident list reads through src.repositories.incidents' own `engine`
    # import, which the shared db_session fixture doesn't patch.
    monkeypatch.setattr("src.repositories.incidents.engine", db_session.get_bind())


def _dismissed(db_session, name, mode, until=None):
    incident = seed_incident(db_session, name)
    incident.dismiss_mode = mode.value
    incident.dismissed_until = until
    db_session.add(incident)
    db_session.commit()
    return incident


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_returns_the_earliest_live_time_boxed_dismissal(db_session, client, test_app):
    now = datetime.now(tz=timezone.utc)
    _dismissed(db_session, "later", IncidentDismissMode.DISMISS_UNTIL, now + timedelta(hours=2))
    _dismissed(db_session, "sooner", IncidentDismissMode.DISMISS_UNTIL, now + timedelta(hours=1))
    _dismissed(db_session, "lapsed", IncidentDismissMode.DISMISS_UNTIL, now - timedelta(minutes=5))
    _dismissed(db_session, "forever", IncidentDismissMode.PERMANENT)
    seed_incident(db_session, "firing")

    response = client.get("/incidents", headers=AUTH, params=NEXT_EXPIRY_QUERY)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [item["user_generated_name"] for item in body["items"]] == ["sooner"]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_nothing_to_wait_for(db_session, client, test_app):
    _dismissed(db_session, "forever", IncidentDismissMode.PERMANENT)
    seed_incident(db_session, "firing")

    response = client.get("/incidents", headers=AUTH, params=NEXT_EXPIRY_QUERY)

    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_sorts_by_dismissed_until_descending(db_session, client, test_app):
    now = datetime.now(tz=timezone.utc)
    _dismissed(db_session, "sooner", IncidentDismissMode.DISMISS_UNTIL, now + timedelta(hours=1))
    _dismissed(db_session, "later", IncidentDismissMode.DISMISS_UNTIL, now + timedelta(hours=2))

    response = client.get(
        "/incidents",
        headers=AUTH,
        params={**NEXT_EXPIRY_QUERY, "limit": 10, "sorting": "-dismissed_until"},
    )

    assert response.status_code == 200
    assert [i["user_generated_name"] for i in response.json()["items"]] == [
        "later",
        "sooner",
    ]
