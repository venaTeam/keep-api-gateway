"""
Tests for the Phase 3 product-BI gateway metrics:
- keep_incident_opened_total (source label) via create_incident_from_dict
- keep_incident_alerts_associated / keep_incident_with_ticket gauges
- keep_login_failures_total
"""

from unittest.mock import patch

import src.repositories.db as db_module
import src.repositories.metrics as metrics_module
import src.services.incident_metrics as incident_metrics_module
from src.models.db.alert import IncidentEnrichment, LastAlertToIncident
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.services.identity_manager.identity_managers.db.db_identitymanager import (
    _record_login_failure,
)
from src.services.incident_metrics import (
    _count_alerts_in_incidents,
    _count_incidents_with_ticket,
    _ticket_provider,
    refresh_incident_metrics,
)

# Note: metric values are asserted by patching the prometheus metric objects and
# checking the .labels()/.inc()/.set() calls rather than reading them back via a
# MultiProcessCollector — multiprocess readback is order-dependent under the full
# suite (the real scrape path is covered separately). create_incident_from_dict
# is reached through db_module so its module-global metric can be patched.
create_incident_from_dict = db_module.create_incident_from_dict


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
    with patch.object(db_module, "incidents_opened_total") as metric:
        _make_incident(db_session)
    metric.labels.assert_called_once_with(
        tenant_id=SINGLE_TENANT_UUID, source="manual"
    )
    metric.labels.return_value.inc.assert_called_once()


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

    with patch.object(
        incident_metrics_module, "incident_alerts_associated_gauge"
    ) as g_alerts, patch.object(
        incident_metrics_module, "incidents_with_ticket_gauge"
    ) as g_ticket:
        refresh_incident_metrics()

    g_alerts.labels.assert_any_call(tenant_id=SINGLE_TENANT_UUID)
    g_alerts.labels.return_value.set.assert_any_call(1)
    g_ticket.labels.assert_any_call(
        tenant_id=SINGLE_TENANT_UUID, ticket_provider="servicenow"
    )
    g_ticket.labels.return_value.set.assert_any_call(1)


def test_login_failure_counter():
    with patch.object(metrics_module, "login_failures_total") as metric:
        _record_login_failure("invalid_credentials")
        _record_login_failure("invalid_credentials")
    metric.labels.assert_called_with(reason="invalid_credentials")
    assert metric.labels.call_count == 2
    assert metric.labels.return_value.inc.call_count == 2
