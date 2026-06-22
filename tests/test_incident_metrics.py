"""
Tests for the Phase 3 product-BI gateway metrics:
- keep_incident_opened_total (source label) via create_incident_from_dict
- keep_incident_alerts_associated / keep_incident_with_ticket gauges
- keep_login_failures_total
"""

from prometheus_client import CollectorRegistry, multiprocess

from src.models.db.alert import IncidentEnrichment, LastAlertToIncident
from src.repositories.db import create_incident_from_dict
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.services.identity_manager.identity_managers.db.db_identitymanager import \
    _record_login_failure
from src.services.incident_metrics import (_count_alerts_in_incidents,
                                           _count_incidents_with_ticket,
                                           _ticket_provider,
                                           refresh_incident_metrics)


def _read_metric(metric_name: str, labels: dict) -> float:
    """Sum the multiprocess samples matching metric_name + labels."""
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    total = 0.0
    found = False
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name != metric_name:
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
                found = True
    return total if found else None


# --- _ticket_provider classification (pure) ---------------------------------


def test_ticket_provider_none_when_no_ticket():
    assert _ticket_provider({}) is None
    assert _ticket_provider({"foo": "bar"}) is None
    assert _ticket_provider(None) is None


def test_ticket_provider_servicenow():
    assert _ticket_provider({"ticket_type": "servicenow"}) == "servicenow"
    assert (
        _ticket_provider({"ticket_url": "https://dev123.service-now.com/x"})
        == "servicenow"
    )


def test_ticket_provider_jira_and_other():
    assert _ticket_provider({"ticket_type": "jira"}) == "jira"
    # has a ticket but unknown provider -> bucketed to "other"
    assert _ticket_provider({"ticket_id": "ABC-1"}) == "other"


# --- DB count helpers -------------------------------------------------------


def _make_incident(session, **overrides):
    data = {
        "user_generated_name": "t",
        "user_summary": "s",
        "generated_summary": "g",
    }
    data.update(overrides)
    return create_incident_from_dict(SINGLE_TENANT_UUID, data, session=session)


def test_count_alerts_in_incidents(db_session):
    incident = _make_incident(db_session)
    db_session.add_all(
        [
            LastAlertToIncident(
                tenant_id=SINGLE_TENANT_UUID,
                fingerprint="fp-1",
                incident_id=incident.id,
            ),
            LastAlertToIncident(
                tenant_id=SINGLE_TENANT_UUID,
                fingerprint="fp-2",
                incident_id=incident.id,
            ),
            # soft-deleted association must NOT be counted
            LastAlertToIncident(
                tenant_id=SINGLE_TENANT_UUID,
                fingerprint="fp-3",
                incident_id=incident.id,
                deleted_at=__import__("datetime").datetime(2025, 1, 1),
            ),
        ]
    )
    db_session.commit()

    counts = _count_alerts_in_incidents(db_session)
    assert counts.get(SINGLE_TENANT_UUID) == 2


def test_count_incidents_with_ticket(db_session):
    inc1 = _make_incident(db_session)
    inc2 = _make_incident(db_session)
    inc3 = _make_incident(db_session)
    db_session.add_all(
        [
            IncidentEnrichment(
                tenant_id=SINGLE_TENANT_UUID,
                incident_id=inc1.id,
                enrichments={"ticket_type": "servicenow", "ticket_url": "x"},
            ),
            IncidentEnrichment(
                tenant_id=SINGLE_TENANT_UUID,
                incident_id=inc2.id,
                enrichments={"ticket_type": "jira"},
            ),
            # no ticket -> not counted
            IncidentEnrichment(
                tenant_id=SINGLE_TENANT_UUID,
                incident_id=inc3.id,
                enrichments={"note": "hello"},
            ),
        ]
    )
    db_session.commit()

    counts = _count_incidents_with_ticket(db_session)
    assert counts.get((SINGLE_TENANT_UUID, "servicenow")) == 1
    assert counts.get((SINGLE_TENANT_UUID, "jira")) == 1
    assert (SINGLE_TENANT_UUID, "other") not in counts


# --- end-to-end metric emission --------------------------------------------


def test_incident_opened_counter_source_manual(db_session):
    before = (
        _read_metric(
            "keep_incident_opened_total",
            {"tenant_id": SINGLE_TENANT_UUID, "source": "manual"},
        )
        or 0.0
    )
    _make_incident(db_session)
    after = _read_metric(
        "keep_incident_opened_total",
        {"tenant_id": SINGLE_TENANT_UUID, "source": "manual"},
    )
    assert after == before + 1


def test_refresh_sets_gauges(db_session):
    incident = _make_incident(db_session)
    db_session.add(
        LastAlertToIncident(
            tenant_id=SINGLE_TENANT_UUID,
            fingerprint="fp-1",
            incident_id=incident.id,
        )
    )
    db_session.add(
        IncidentEnrichment(
            tenant_id=SINGLE_TENANT_UUID,
            incident_id=incident.id,
            enrichments={"ticket_type": "servicenow"},
        )
    )
    db_session.commit()

    refresh_incident_metrics()

    assert (
        _read_metric(
            "keep_incident_alerts_associated", {"tenant_id": SINGLE_TENANT_UUID}
        )
        == 1
    )
    assert (
        _read_metric(
            "keep_incident_with_ticket",
            {"tenant_id": SINGLE_TENANT_UUID, "ticket_provider": "servicenow"},
        )
        == 1
    )


def test_login_failure_counter():
    before = (
        _read_metric("keep_login_failures_total", {"reason": "invalid_credentials"})
        or 0.0
    )
    _record_login_failure("invalid_credentials")
    _record_login_failure("invalid_credentials")
    after = _read_metric("keep_login_failures_total", {"reason": "invalid_credentials"})
    assert after == before + 2
