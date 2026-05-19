import time
from datetime import datetime

import pytest

from src.models.alert import AlertStatus
from tests.fixtures.client import client, setup_api_key, test_app  # noqa


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_basic(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test basic batch enrichment with a simple CEL expression (name matching)."""
    # Create test alerts with specific names
    create_alert(
        "alert-cpu-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "CPU Overload Alert", "severity": "critical"},
    )
    create_alert(
        "alert-memory-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Memory Usage Alert", "severity": "warning"},
    )
    create_alert(
        "alert-disk-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Disk Space Alert", "severity": "warning"},
    )

    # Enrich alerts with name containing "CPU" via CEL
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "name.contains('CPU')",
            "enrichments": {
                "status": "acknowledged",
                "note": "CPU issue being investigated",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"

    # Verify event was sent
    assert len(client.mock_producer.produced_events) == 1
    event_data = client.mock_producer.produced_events[0]
    assert event_data["event_type"].value == "batch_enrich"
    
    # Verify the fingerprint of the CPU alert was used
    # Find the CPU alert fingerprint from DB
    response = client.get("/preset/feed/alerts", headers={"x-api-key": "some-key"})
    alerts = response.json()
    cpu_alert = next(a for a in alerts if a["name"] == "CPU Overload Alert")
    
    assert event_data["fingerprint"] == [cpu_alert["fingerprint"]]
    assert event_data["event"]["status"] == "acknowledged"
    assert event_data["event"]["note"] == "CPU issue being investigated"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_severity(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test batch enrichment with CEL expression filtering by severity."""
    # Create test alerts with different severities
    create_alert(
        "alert-critical-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Critical Service Down", "severity": "critical"},
    )
    create_alert(
        "alert-warning-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Warning Alert", "severity": "warning"},
    )
    create_alert(
        "alert-warning-2",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Another Warning", "severity": "warning"},
    )

    # Enrich all warning alerts
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "severity == 'warning'",
            "enrichments": {
                "status": "suppressed",
                "note": "Low priority alerts suppressed",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"

    # Verify event was sent
    assert len(client.mock_producer.produced_events) == 1
    event_data = client.mock_producer.produced_events[0]
    assert event_data["event_type"].value == "batch_enrich"
    
    # Verify the fingerprint of the warning alerts were used
    response = client.get("/preset/feed/alerts", headers={"x-api-key": "some-key"})
    alerts = response.json()
    warning_alerts = [a for a in alerts if a["severity"] == "warning"]
    
    # order of fingerprints might vary depending on DB fetch
    expected_fingerprints = sorted([a["fingerprint"] for a in warning_alerts])
    actual_fingerprints = sorted(event_data["fingerprint"])
    
    assert expected_fingerprints == actual_fingerprints
    assert event_data["event"]["status"] == "suppressed"
    assert event_data["event"]["note"] == "Low priority alerts suppressed"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_labels(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test batch enrichment with CEL expression filtering by labels."""
    # Create test alerts with different labels
    create_alert(
        "alert-region1-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Region 1 Alert",
            "severity": "critical",
            "labels": {"region": "us-east-1", "service": "api"},
        },
    )
    create_alert(
        "alert-region1-2",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Region 1 Service Alert",
            "severity": "warning",
            "labels": {"region": "us-east-1", "service": "database"},
        },
    )
    create_alert(
        "alert-region2-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Region 2 Alert",
            "severity": "critical",
            "labels": {"region": "us-west-1", "service": "api"},
        },
    )

    # Enrich alerts from us-east-1 region
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "labels.region == 'us-east-1'",
            "enrichments": {
                "status": "acknowledged",
                "assignee": "east-team@example.com",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"

    # Verify event was sent
    assert len(client.mock_producer.produced_events) == 1
    event_data = client.mock_producer.produced_events[0]
    assert event_data["event_type"].value == "batch_enrich"
    
    response = client.get("/preset/feed/alerts", headers={"x-api-key": "some-key"})
    alerts = response.json()
    east_alerts = [a for a in alerts if a.get("labels", {}).get("region") == "us-east-1"]
    
    expected_fingerprints = sorted([a["fingerprint"] for a in east_alerts])
    actual_fingerprints = sorted(event_data["fingerprint"])
    
    assert expected_fingerprints == actual_fingerprints
    assert event_data["event"]["status"] == "acknowledged"
    assert event_data["event"]["assignee"] == "east-team@example.com"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_complex_expression(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test batch enrichment with a complex CEL expression combining multiple conditions."""
    # Create test alerts with various properties
    create_alert(
        "alert-prod-critical-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Production Critical Alert",
            "severity": "critical",
            "environment": "production",
            "service": "api",
        },
    )
    create_alert(
        "alert-prod-warning-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Production Warning Alert",
            "severity": "warning",
            "environment": "production",
            "service": "api",
        },
    )
    create_alert(
        "alert-staging-critical-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Staging Critical Alert",
            "severity": "critical",
            "environment": "staging",
            "service": "api",
        },
    )
    create_alert(
        "alert-prod-critical-2",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {
            "name": "Production Critical DB Alert",
            "severity": "critical",
            "environment": "production",
            "service": "database",
        },
    )

    # Enrich critical production API alerts
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "severity == 'critical' && environment == 'production' && service == 'api'",
            "enrichments": {
                "status": "acknowledged",
                "note": "Critical API issue - investigating",
                "assignee": "api-team@example.com",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"

    # Verify event was sent
    assert len(client.mock_producer.produced_events) == 1
    event_data = client.mock_producer.produced_events[0]
    assert event_data["event_type"].value == "batch_enrich"

    response = client.get("/preset/feed/alerts", headers={"x-api-key": "some-key"})
    alerts = response.json()
    prod_critical_api = next(
        a
        for a in alerts
        if a["environment"] == "production"
        and a["severity"] == "critical"
        and a["service"] == "api"
    )

    assert event_data["fingerprint"] == [prod_critical_api["fingerprint"]]
    assert event_data["event"]["status"] == "acknowledged"
    assert event_data["event"]["note"] == "Critical API issue - investigating"
    assert event_data["event"]["assignee"] == "api-team@example.com"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_no_matching_alerts(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test batch enrichment when no alerts match the CEL expression."""
    # Create some alerts
    create_alert(
        "alert-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Test Alert 1", "severity": "critical"},
    )
    create_alert(
        "alert-2",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Test Alert 2", "severity": "warning"},
    )

    # Use a CEL expression that won't match any alerts
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "name.contains('NonExistentString')",
            "enrichments": {
                "status": "acknowledged",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    assert "message" in result
    assert "No alerts matched the query" in result["message"]

    # Verify no alerts were changed
    response = client.get(
        "/preset/feed/alerts",
        headers={"x-api-key": "some-key"},
    )
    alerts = response.json()

    assert len(alerts) == 2
    assert all(a["status"] == "firing" for a in alerts)


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_invalid_expression(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test batch enrichment with an invalid CEL expression."""
    # Create an alert
    create_alert(
        "alert-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Test Alert", "severity": "critical"},
    )

    # Use an invalid CEL expression
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "invalid.syntax &&& !!!",
            "enrichments": {
                "status": "acknowledged",
            },
        },
    )

    # Should return a 400 error
    assert response.status_code == 400
    assert "Error parsing CEL expression" in response.json()["detail"]

    # Verify no alerts were changed
    response = client.get(
        "/preset/feed/alerts",
        headers={"x-api-key": "some-key"},
    )
    alerts = response.json()

    assert len(alerts) == 1
    assert alerts[0]["status"] == "firing"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_batch_enrich_cel_dispose_on_new_alert(
    db_session, client, test_app, create_alert, elastic_client
):
    """Test batch enrichment with dispose_on_new_alert parameter."""
    # Create an alert
    create_alert(
        "alert-test-1",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Test Alert", "severity": "critical"},
    )

    # Enrich the alert with dispose_on_new_alert=True
    response = client.post(
        "/alerts/batch_enrich?dispose_on_new_alert=true",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "name.contains('Test')",
            "enrichments": {
                "status": "resolved",
                "note": "Temporary resolution note",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"

    # Verify the events were sent
    assert len(client.mock_producer.produced_events) == 1
    
    event_data_1 = client.mock_producer.produced_events[0]
    assert event_data_1["event_type"].value == "batch_enrich"
    assert event_data_1["event"]["disposable_status"]["value"] == "resolved"
    assert event_data_1["event"]["disposable_note"]["value"] == "Temporary resolution note"
