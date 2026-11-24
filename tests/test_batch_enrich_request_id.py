"""Every log line of a batch enrichment carries the caller's X-Request-ID.

keep-assistant sends the draft id there, so one id finds a change in both services.

The requests run in an empty `contextvars.Context()`: the autouse `mocked_context`
fixture opens a request context with an integer id, and the point here is that the
id comes from the request itself.
"""

import contextvars
import logging
import uuid
from datetime import datetime

import pytest

from src.models.alert import AlertStatus
from src.repositories.dependencies import GENERIC_TENANT_UUID
from tests.fixtures.client import client, setup_api_key, test_app  # noqa
from tests.fixtures.identity import bearer_headers

REQUEST_ID = "3f2c5a1e-8b7d-4c69-9a51-0d2e4f6a8b1c"

BY_FINGERPRINT = {
    "fingerprints": ["fp-1", "fp-2"],
    "enrichments": {"status": "acknowledged"},
}
BY_CEL = {"cel": "name == 'one'", "enrichments": {"status": "acknowledged"}}


def _post(client, body, request_id=REQUEST_ID):
    headers = bearer_headers("noc")
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return contextvars.Context().run(
        client.post, "/alerts/batch_enrich", headers=headers, json=body
    )


def _route_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == "src.routes.alerts"
        and record.funcName == "batch_enrich_alerts"
    ]


@pytest.fixture
def seeded(create_alert, caplog):
    for fingerprint, name in (("fp-1", "one"), ("fp-2", "two")):
        create_alert(
            fingerprint, AlertStatus.FIRING, datetime.utcnow(), {"name": name}
        )
    caplog.set_level(logging.INFO)
    caplog.clear()


@pytest.mark.parametrize("body", [BY_FINGERPRINT, BY_CEL], ids=["fingerprints", "cel"])
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_success_logs_carry_the_request_id(
    db_session, client, test_app, seeded, caplog, body
):
    response = _post(client, body)

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert response.headers["X-Request-ID"] == REQUEST_ID
    records = _route_records(caplog)
    messages = [record.getMessage() for record in records]
    assert "Alerts batch enriched successfully" in messages
    assert [getattr(record, "request_id", None) for record in records] == [
        REQUEST_ID
    ] * len(records), messages


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_failure_log_carries_the_request_id_tenant_and_count(
    db_session, client, test_app, seeded, caplog, monkeypatch
):
    from src.routes import alerts as alerts_routes

    async def fail(*args, **kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(alerts_routes.EnrichmentsBl, "batch_enrich", fail)

    response = _post(client, BY_FINGERPRINT)

    assert (response.status_code, response.json()) == (200, {"status": "failed"})
    (record,) = [
        r
        for r in _route_records(caplog)
        if r.getMessage() == "Failed to enrich alerts batch"
    ]
    assert record.levelno == logging.ERROR
    assert record.request_id == REQUEST_ID
    assert record.tenant_id == GENERIC_TENANT_UUID
    assert record.fingerprint_count == 2
    assert record.error == "db went away"


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_cel_failure_and_no_match_logs_carry_the_request_id(
    db_session, client, test_app, seeded, caplog, monkeypatch
):
    _post(client, {**BY_CEL, "cel": "name == 'nothing matches'"})
    assert "No alerts found matching the CEL query" in [
        r.getMessage() for r in _route_records(caplog)
    ]

    # An invalid filter, then a selection that breaks for another reason.
    assert _post(client, {**BY_CEL, "cel": "name =="}).status_code == 400

    from src.routes import alerts as alerts_routes

    def fail(*args, **kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(alerts_routes, "query_last_alerts", fail)
    assert _post(client, BY_CEL).json()["status"] == "failed"

    records = _route_records(caplog)
    assert "Failed to process CEL query" in [r.getMessage() for r in records]
    assert {getattr(r, "request_id", None) for r in records} == {REQUEST_ID}


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_failed_side_effects_carry_the_request_id(
    db_session, client, test_app, seeded, caplog, monkeypatch
):
    from src.routes import alerts as alerts_routes

    def fail(*args, **kwargs):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(alerts_routes, "ElasticClient", fail)
    monkeypatch.setattr(alerts_routes, "notify_sse", fail)

    # EnrichmentsBl builds its own client, so the write itself still succeeds.
    assert _post(client, BY_FINGERPRINT).json() == {"status": "ok"}

    records = _route_records(caplog)
    messages = [r.getMessage() for r in records]
    assert "Failed to push alerts to elasticsearch" in messages
    assert "Failed to tell client to poll alerts" in messages
    assert {getattr(r, "request_id", None) for r in records} == {REQUEST_ID}


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_request_without_the_header_logs_the_generated_id(
    db_session, client, test_app, seeded, caplog
):
    response = _post(client, BY_FINGERPRINT, request_id=None)

    generated = response.headers["X-Request-ID"]
    uuid.UUID(generated)
    assert {r.request_id for r in _route_records(caplog)} == {generated}


@pytest.mark.parametrize("bad", ["not-a-uuid", "draft:" + REQUEST_ID, "1"])
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_non_uuid_is_refused_before_the_route(
    db_session, client, test_app, seeded, caplog, bad
):
    """Existing plugin behaviour callers depend on: an empty-body 400."""
    response = _post(client, BY_FINGERPRINT, request_id=bad)

    assert response.status_code == 400
    assert response.content == b""
    assert _route_records(caplog) == []


def test_current_request_id_outside_a_request_is_none():
    from src.utils.request_id import current_request_id

    assert contextvars.Context().run(current_request_id) is None
