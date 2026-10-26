from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from src.models.db.incident import IncidentStatus
from src.models.query import CountQueryDto
from src.repositories.alerts import query_missing_alerts_count
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.alert_graph import link, seed_alert, seed_incident

OTHER_TENANT = "other-tenant"
REMOVED = datetime(2025, 1, 1)


def missing(tenant_id=SINGLE_TENANT_UUID, **query):
    return query_missing_alerts_count(
        tenant_id=tenant_id, query=CountQueryDto(include_missing=True, **query)
    )


@pytest.fixture
def scene(db_session):
    """Eight alerts; four have no incident and four have no service.

    no incident: fp-removed-link, fp-hidden-only, fp-candidate-only, fp-none
    no service:  fp-ack (''), fp-removed-link, fp-candidate-only, fp-none
    fp-hidden-only has service ' ', which is a value.
    """
    firing = seed_incident(db_session, "firing")
    other_firing = seed_incident(db_session, "other-firing")
    resolved = seed_incident(db_session, "resolved", IncidentStatus.RESOLVED)
    acknowledged = seed_incident(db_session, "ack", IncidentStatus.ACKNOWLEDGED)
    hidden = seed_incident(db_session, "hidden", is_visible=False)
    candidate = seed_incident(db_session, "candidate", is_candidate=True)

    seed_alert(db_session, "fp-in-firing", service="s1")
    link(db_session, "fp-in-firing", firing)

    seed_alert(db_session, "fp-in-two", service="s1")
    link(db_session, "fp-in-two", firing)
    link(db_session, "fp-in-two", other_firing)

    seed_alert(db_session, "fp-resolved-only", service="s2")
    link(db_session, "fp-resolved-only", resolved)

    seed_alert(db_session, "fp-ack", service="")
    link(db_session, "fp-ack", acknowledged)

    seed_alert(db_session, "fp-removed-link")
    link(db_session, "fp-removed-link", firing, deleted_at=REMOVED)

    seed_alert(db_session, "fp-hidden-only", service=" ")
    link(db_session, "fp-hidden-only", hidden)

    seed_alert(db_session, "fp-candidate-only")
    link(db_session, "fp-candidate-only", candidate)

    seed_alert(db_session, "fp-none", name="solo")


def test_alerts_with_no_incident_are_missing(scene):
    assert missing(group_by="incident") == 4


def test_an_alert_only_in_a_resolved_incident_is_not_missing(db_session):
    resolved = seed_incident(db_session, "resolved", IncidentStatus.RESOLVED)
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", resolved)

    assert missing(group_by="incident") == 0


def test_a_deleted_incident_does_not_count_as_an_incident(db_session):
    deleted = seed_incident(db_session, "deleted", IncidentStatus.DELETED)
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", deleted)

    assert missing(group_by="incident") == 1


def test_hidden_and_candidate_incidents_do_not_count_as_an_incident(db_session):
    hidden = seed_incident(db_session, "hidden", is_visible=False)
    candidate = seed_incident(db_session, "candidate", is_candidate=True)
    seed_alert(db_session, "fp-hidden")
    link(db_session, "fp-hidden", hidden)
    seed_alert(db_session, "fp-candidate")
    link(db_session, "fp-candidate", candidate)

    assert missing(group_by="incident") == 2


def test_a_removed_link_does_not_count_as_an_incident(db_session):
    incident = seed_incident(db_session, "firing")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", incident, deleted_at=REMOVED)

    assert missing(group_by="incident") == 1


@pytest.mark.parametrize("incident_status", ["active", "firing", "acknowledged"])
def test_incident_status_does_not_change_missing(scene, incident_status):
    assert missing(group_by="incident", incident_status=incident_status) == 4


def test_empty_field_values_are_missing(scene):
    assert missing(group_by="service") == 4


def test_missing_respects_the_cel_filter(scene):
    assert missing(group_by="incident", cel='name == "solo"') == 1
    assert missing(group_by="service", cel='name == "solo"') == 1
    assert missing(group_by="incident", cel='name == "nobody"') == 0


def test_cel_mentioning_incident_counts_an_alert_once(db_session):
    first = seed_incident(db_session, "first")
    second = seed_incident(db_session, "second")
    seed_alert(db_session, "fp-1", name="solo")
    link(db_session, "fp-1", first, deleted_at=REMOVED)
    link(db_session, "fp-1", second, deleted_at=REMOVED)

    assert (
        missing(group_by="incident", cel='name == "solo" || incident.name == "zzz"')
        == 1
    )


def test_other_tenant_alerts_are_not_missing(db_session):
    seed_alert(db_session, "fp-foreign", tenant_id=OTHER_TENANT)

    assert missing(group_by="incident") == 0
    assert missing(tenant_id=OTHER_TENANT, group_by="incident") == 1


def test_a_database_failure_is_raised_not_hidden_as_zero(db_session):
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with patch(
        "src.repositories.alerts.build_missing_count_query", side_effect=failure
    ):
        with pytest.raises(OperationalError):
            missing(group_by="incident")
