"""Which `status` a caller may send when creating or updating an incident.

`status` is not a declared field on IncidentDtoIn, so it used to be stored as
whatever was sent: 'deleted' was saved, read back as firing, and showed up under
its own 'deleted' facet. Firing, acknowledged and resolved are accepted on both
routes; suppressed and merged have their own flows, and anything else is not a
status at all.
"""

import pytest
from sqlmodel import select

from src.models.db.incident import Incident
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
