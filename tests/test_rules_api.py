import pytest

from src.models.db.incident import Incident
from src.repositories.db import create_rule as create_rule_db
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

TEST_RULE_DATA = {
    "tenant_id": SINGLE_TENANT_UUID,
    "name": "test-rule",
    "definition": {
        "sql": "N/A",  # we don't use it anymore
        "params": {},
    },
    "timeframe": 600,
    "timeunit": "seconds",
    "definition_cel": '(source == "sentry") || (source == "grafana" && severity == "critical")',
    "created_by": "test@keephq.dev",
}

INVALID_DATA_STEPS = [
    {
        "update": {"sqlQuery": {"sql": "", "params": []}},
        "error": "SQL is required",
    },
    {
        "update": {"sqlQuery": {"sql": "SELECT", "params": []}},
        "error": "Params are required",
    },
    {
        "update": {"celQuery": ""},
        "error": "CEL is required",
    },
    {
        "update": {"ruleName": ""},
        "error": "Rule name is required",
    },
    {
        "update": {"timeframeInSeconds": 0},
        "error": "Timeframe is required",
    },
    {
        "update": {"timeUnit": ""},
        "error": "Timeunit is required",
    },
]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_get_rules_api(db_session, client, test_app):
    rule = create_rule_db(**TEST_RULE_DATA)

    response = client.get(
        "/rules",
        headers={"x-api-key": "some-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == str(rule.id)

    rule2 = create_rule_db(**TEST_RULE_DATA)

    response2 = client.get(
        "/rules",
        headers={"x-api-key": "some-key"},
    )

    assert response2.status_code == 200
    data = response2.json()
    assert len(data) == 2
    assert data[0]["id"] == str(rule.id)
    assert data[1]["id"] == str(rule2.id)


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_create_rule_api(db_session, client, test_app):
    rule_data = {
        "ruleName": "test rule",
        "sqlQuery": {
            "sql": "SELECT * FROM alert where severity = %s",
            "params": ["critical"],
        },
        "celQuery": "severity = 'critical'",
        "timeframeInSeconds": 300,
        "timeUnit": "seconds",
        "requireApprove": False,
    }

    response = client.post("/rules", headers={"x-api-key": "some-key"}, json=rule_data)

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test rule"
    assert data["definition_cel"] == "severity = 'critical'"

    invalid_rule_data = {k: v for k, v in rule_data.items() if k != "ruleName"}

    invalid_data_response = client.post(
        "/rules", headers={"x-api-key": "some-key"}, json=invalid_rule_data
    )

    assert invalid_data_response.status_code == 422
    data = invalid_data_response.json()
    assert "detail" in data
    assert len(data["detail"]) == 1
    assert data["detail"][0]["loc"] == ["body", "ruleName"]
    assert data["detail"][0]["msg"] == "field required"

    for invalid_data_step in INVALID_DATA_STEPS:
        current_step = "Invalid data step: {}".format(invalid_data_step["error"])
        invalid_data_response_2 = client.post(
            "/rules",
            headers={"x-api-key": "some-key"},
            json=dict(rule_data, **invalid_data_step["update"]),
        )

        assert invalid_data_response_2.status_code == 400, current_step
        data = invalid_data_response_2.json()
        assert "detail" in data, current_step
        assert data["detail"] == invalid_data_step["error"], current_step


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_delete_rule_api(db_session, client, test_app):
    rule = create_rule_db(**TEST_RULE_DATA)

    response = client.delete(
        "/rules/{}".format(rule.id),
        headers={"x-api-key": "some-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert data["message"] == "Rule deleted"

    response = client.delete(
        "/rules/{}".format(rule.id),
        headers={"x-api-key": "some-key"},
    )

    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    assert data["detail"] == "Rule not found"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_rule_api(db_session, client, test_app):
    rule = create_rule_db(**TEST_RULE_DATA)

    rule_data = {
        "ruleName": "test rule",
        "sqlQuery": {
            "sql": "SELECT * FROM alert where severity = %s",
            "params": ["critical"],
        },
        "celQuery": "severity = 'critical'",
        "timeframeInSeconds": 300,
        "timeUnit": "seconds",
        "requireApprove": False,
        "resolveOn": "all_resolved",
        "createOn": "any",
    }

    response = client.put(
        "/rules/{}".format(rule.id), headers={"x-api-key": "some-key"}, json=rule_data
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test rule"
    assert data["definition_cel"] == "severity = 'critical'"

    for invalid_data_step in INVALID_DATA_STEPS:
        current_step = "Invalid data step: {}".format(invalid_data_step["error"])
        invalid_data_response_2 = client.put(
            "/rules/{}".format(rule.id),
            headers={"x-api-key": "some-key"},
            json=dict(rule_data, **invalid_data_step["update"]),
        )

        assert invalid_data_response_2.status_code == 400, current_step
        data = invalid_data_response_2.json()
        assert "detail" in data, current_step
        assert data["detail"] == invalid_data_step["error"], current_step


BASE = {
    "ruleName": "test rule",
    "sqlQuery": {
        "sql": "SELECT * FROM alert where severity = %s",
        "params": ["critical"],
    },
    "celQuery": 'severity == "critical"',
    "timeframeInSeconds": 300,
    "timeUnit": "seconds",
    "requireApprove": False,  # PUT /rules/{id} defaults a missing value to [] and 500s
    "createOn": "any",
}
H = {"x-api-key": "some-key"}


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("value", ["never", "all_resolved"])
def test_create_rule_accepts_selectable_resolve_on(db_session, client, test_app, value):
    r = client.post("/rules", headers=H, json={**BASE, "resolveOn": value})
    assert r.status_code == 200 and r.json()["resolve_on"] == value


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "value", ["first_resolved", "last_resolved", "all", "ALL_RESOLVED"]
)
def test_create_rule_rejects_unsupported_resolve_on(
    db_session, client, test_app, value
):
    r = client.post("/rules", headers=H, json={**BASE, "resolveOn": value})
    assert r.status_code == 400
    assert (
        r.json()["detail"]
        == f"resolveOn '{value}' is not supported; use 'never' or 'all_resolved'"
    )
    assert client.get("/rules", headers=H).json() == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_legacy_rule_must_migrate_on_edit(db_session, client, test_app):
    rule = create_rule_db(**TEST_RULE_DATA, resolve_on="first_resolved")
    # still readable
    assert client.get("/rules", headers=H).json()[0]["resolve_on"] == "first_resolved"
    r = client.put(
        f"/rules/{rule.id}",
        headers=H,
        json={**BASE, "ruleName": "renamed", "resolveOn": "first_resolved"},
    )
    assert r.status_code == 400
    got = client.get("/rules", headers=H).json()[0]
    # nothing written
    assert (got["name"], got["resolve_on"]) == ("test-rule", "first_resolved")
    r = client.put(
        f"/rules/{rule.id}",
        headers=H,
        json={**BASE, "ruleName": "renamed", "resolveOn": "all_resolved"},
    )
    assert r.status_code == 200
    got = client.get("/rules", headers=H).json()[0]
    assert (got["name"], got["resolve_on"]) == ("renamed", "all_resolved")


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_editing_legacy_rule_leaves_its_incidents_alone(db_session, client, test_app):
    rule = create_rule_db(**TEST_RULE_DATA, resolve_on="first_resolved")
    incident = Incident(
        tenant_id=SINGLE_TENANT_UUID,
        rule_id=rule.id,
        resolve_on="first_resolved",
        user_generated_name="legacy incident",
    )
    db_session.add(incident)
    db_session.commit()
    r = client.put(
        f"/rules/{rule.id}", headers=H, json={**BASE, "resolveOn": "all_resolved"}
    )
    assert r.status_code == 200, r.text
    db_session.refresh(incident)
    assert incident.resolve_on == "first_resolved"
