import time
from datetime import datetime

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import LastAlert
from tests.fixtures.client import client, setup_api_key, test_app  # noqa


def _last_alerts(db_session) -> dict:
    """LastAlert rows by fingerprint, re-read from the DB."""
    db_session.expire_all()
    return {la.fingerprint: la for la in db_session.query(LastAlert).all()}


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

    # Only the CPU alert was written
    rows = _last_alerts(db_session)
    assert rows["alert-cpu-1"].status == "acknowledged"
    assert rows["alert-cpu-1"].note == "CPU issue being investigated"
    for fingerprint in ("alert-memory-1", "alert-disk-1"):
        assert rows[fingerprint].status is None
        assert rows[fingerprint].note is None


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

    # Only the warning alerts were written
    rows = _last_alerts(db_session)
    for fingerprint in ("alert-warning-1", "alert-warning-2"):
        assert rows[fingerprint].status == "suppressed"
        assert rows[fingerprint].note == "Low priority alerts suppressed"
    assert rows["alert-critical-1"].status is None
    assert rows["alert-critical-1"].note is None


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

    # Strict schema: dynamic enrichment fields like `labels.region` have
    # no destination and are discarded, so CEL can only filter typed columns. Filter
    # by the typed `severity` column instead.
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "severity == 'critical'",
            "enrichments": {
                "status": "acknowledged",
                "assignee": "east-team@example.com",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"

    # Only the critical alerts were written
    rows = _last_alerts(db_session)
    for fingerprint in ("alert-region1-1", "alert-region2-1"):
        assert rows[fingerprint].status == "acknowledged"
        assert rows[fingerprint].assignee == "east-team@example.com"
    assert rows["alert-region1-2"].status is None
    assert rows["alert-region1-2"].assignee is None


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

    # Strict schema: only typed columns are CEL-filterable; the dynamic
    # `environment` field is discarded. Filter by the typed `severity` + `service`
    # columns (matches the two critical/api alerts).
    response = client.post(
        "/alerts/batch_enrich",
        headers={"x-api-key": "some-key"},
        json={
            "cel": "severity == 'critical' && service == 'api'",
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

    # Only the critical api alerts were written
    rows = _last_alerts(db_session)
    for fingerprint in ("alert-prod-critical-1", "alert-staging-critical-1"):
        assert rows[fingerprint].status == "acknowledged"
        assert rows[fingerprint].note == "Critical API issue - investigating"
        assert rows[fingerprint].assignee == "api-team@example.com"
    for fingerprint in ("alert-prod-warning-1", "alert-prod-critical-2"):
        assert rows[fingerprint].status is None
        assert rows[fingerprint].assignee is None


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

    # Should return the structured invalid-CEL 400
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "INVALID_CEL"
    assert detail["diagnostics"]

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

    # "dispose on new alert" is the typed status_disposable flag (cleared on the
    # next non-resolved re-fire in set_last_alert).
    row = _last_alerts(db_session)["alert-test-1"]
    assert row.status == "resolved"
    assert row.note == "Temporary resolution note"
    assert row.status_disposable is True
