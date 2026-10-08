from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.models.db.incident import IncidentStatus
from tests.fixtures.alert_graph import link, seed_alert, seed_incident
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

AUTH = {"x-api-key": "some-key"}
COUNT_URL = "/alerts/query/count"


@pytest.fixture
def two_incidents(db_session):
    first = seed_incident(db_session, "first")
    second = seed_incident(db_session, "second", IncidentStatus.RESOLVED)
    for index in range(3):
        fingerprint = f"fp-first-{index}"
        seed_alert(db_session, fingerprint, service="checkout")
        link(db_session, fingerprint, first)
    seed_alert(db_session, "fp-second", service="search")
    link(db_session, "fp-second", second)
    seed_alert(db_session, "fp-loose", service="search")


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ungrouped_count_still_counts_alerts(
    db_session, client, test_app, two_incidents
):
    response = client.post(COUNT_URL, headers=AUTH, json={"cel": ""})

    assert response.status_code == 200
    assert response.json() == 5


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "body, expected",
    [
        ({"group_by": "incident"}, 1),
        ({"group_by": "incident", "incident_status": "firing"}, 1),
        ({"group_by": "incident", "incident_status": "acknowledged"}, 0),
        ({"group_by": "service"}, 2),
        ({"group_by": "service", "cel": "service == 'search'"}, 1),
    ],
)
def test_grouped_count_returns_a_bare_integer(
    db_session, client, test_app, two_incidents, body, expected
):
    response = client.post(COUNT_URL, headers=AUTH, json={"cel": "", **body})

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "body",
    [
        {"group_by": "severity"},
        {"group_by": "labels.team"},
        {"incident_status": "active"},
        {"group_by": "service", "incident_status": "active"},
        {"group_by": "incident", "incident_status": "resolved"},
    ],
)
def test_invalid_grouping_is_a_validation_error(db_session, client, test_app, body):
    response = client.post(COUNT_URL, headers=AUTH, json={"cel": "", **body})

    assert response.status_code == 422
    assert "INVALID_CEL" not in response.text


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_invalid_cel_with_grouping_is_still_a_cel_error(db_session, client, test_app):
    response = client.post(
        COUNT_URL,
        headers=AUTH,
        json={"cel": "no_such_field == 'x'", "group_by": "incident"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CEL"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_grouped_count_surfaces_a_database_failure(db_session, client, test_app):
    failing_client = TestClient(client.app, raise_server_exceptions=False)
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with patch(
        "src.repositories.alerts.build_distinct_count_query", side_effect=failure
    ):
        response = failing_client.post(
            COUNT_URL, headers=AUTH, json={"cel": "", "group_by": "incident"}
        )

    assert response.status_code == 500
    assert "INVALID_CEL" not in response.text
