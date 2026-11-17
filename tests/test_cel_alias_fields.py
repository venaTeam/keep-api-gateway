"""The alerts table shows `component`, `lastReceived` and `incident`; a CEL
filter on those names must work like the stored names they stand for."""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from src.models.db.alert import LastAlert
from src.models.query import QueryDto
from src.repositories.alerts import (
    get_alert_potential_facet_fields,
    query_last_alerts,
    query_total_alerts_count,
)
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.alert_graph import link, seed_alert, seed_incident


def fingerprints(cel: str) -> set[str]:
    alerts = query_last_alerts(SINGLE_TENANT_UUID, QueryDto(cel=cel))
    return {alert.fingerprint for alert in alerts}


@pytest.fixture
def alerts(db_session):
    db = seed_alert(db_session, "fp-db", name="db")
    db.object = "postgres"
    seed_alert(db_session, "fp-web", name="web")

    old = datetime.utcnow() - timedelta(days=2)
    last_alert = db_session.exec(
        select(LastAlert).where(LastAlert.fingerprint == "fp-web")
    ).one()
    last_alert.last_received = old
    db_session.add_all([db, last_alert])
    db_session.commit()

    incident = seed_incident(db_session, "outage")
    link(db_session, "fp-db", incident)
    return db_session


def test_component_filters_on_object(alerts):
    assert fingerprints('component == "postgres"') == {"fp-db"}
    assert fingerprints('component == "postgres"') == fingerprints(
        'object == "postgres"'
    )


def test_last_received_camel_case(alerts):
    since = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert fingerprints(f'lastReceived < "{since}"') == {"fp-web"}
    assert fingerprints(f'lastReceived < "{since}"') == fingerprints(
        f'last_received < "{since}"'
    )


def test_incident_filters_on_incident_name(alerts):
    assert fingerprints('incident == "outage"') == {"fp-db"}
    assert fingerprints('incident.name == "outage"') == {"fp-db"}


def test_incident_total_counts_each_alert_once(alerts):
    """An alert in two matching incidents is two joined rows, one alert."""
    link(alerts, "fp-db", seed_incident(alerts, "outage"))

    total = query_total_alerts_count(
        SINGLE_TENANT_UUID, QueryDto(cel='incident == "outage"')
    )
    assert total == 1


def test_component_is_offered_as_a_field(alerts):
    assert "component" in get_alert_potential_facet_fields(SINGLE_TENANT_UUID)
