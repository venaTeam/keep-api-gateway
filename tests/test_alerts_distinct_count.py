from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from src.models.db.incident import IncidentStatus
from src.models.query import CountQueryDto
from src.repositories.alerts import query_distinct_alerts_count
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.alert_graph import link, seed_alert, seed_incident

OTHER_TENANT = "other-tenant"


def count(tenant_id=SINGLE_TENANT_UUID, **query):
    return query_distinct_alerts_count(
        tenant_id=tenant_id, query=CountQueryDto(**query)
    )


@pytest.fixture
def graph(db_session):
    """Seven live incidents, one resolved, one unlinked, one overridden each way.

    active:       busy, two-firing-a, two-firing-b, current-firing,
                  override-firing, acknowledged, quiet            -> 7
    firing only:  the same without acknowledged                    -> 6
    acknowledged: acknowledged                                     -> 1
    """
    busy = seed_incident(db_session, "busy")
    for index in range(40):
        fingerprint = f"fp-busy-{index}"
        seed_alert(
            db_session,
            fingerprint,
            name="n1",
            service="s1",
            assignee="u1" if index == 0 else None,
        )
        link(db_session, fingerprint, busy)

    two_firing_a = seed_incident(db_session, "two-firing-a")
    two_firing_b = seed_incident(db_session, "two-firing-b")
    seed_alert(
        db_session,
        "fp-two",
        name="n2",
        service="s2",
        assignee="u2",
        node_name="h1",
        application="app1",
        site="eu",
    )
    link(db_session, "fp-two", two_firing_a)
    link(db_session, "fp-two", two_firing_b)

    old_resolved = seed_incident(db_session, "old-resolved", IncidentStatus.RESOLVED)
    current_firing = seed_incident(db_session, "current-firing")
    seed_alert(db_session, "fp-mixed", name="n2", node_name="h2")
    link(db_session, "fp-mixed", old_resolved)
    link(db_session, "fp-mixed", current_firing)

    unlinked = seed_incident(db_session, "unlinked")
    seed_alert(db_session, "fp-unlinked", name="n2")
    link(db_session, "fp-unlinked", unlinked, deleted_at=datetime(2025, 1, 1))

    override_resolved = seed_incident(
        db_session,
        "override-resolved",
        IncidentStatus.FIRING,
        enrichment_status=IncidentStatus.RESOLVED,
    )
    seed_alert(db_session, "fp-override-resolved", name="n2")
    link(db_session, "fp-override-resolved", override_resolved)

    override_firing = seed_incident(
        db_session,
        "override-firing",
        IncidentStatus.RESOLVED,
        enrichment_status=IncidentStatus.FIRING,
    )
    seed_alert(db_session, "fp-override-firing", name="n2")
    link(db_session, "fp-override-firing", override_firing)

    acknowledged = seed_incident(db_session, "acknowledged", IncidentStatus.ACKNOWLEDGED)
    seed_alert(db_session, "fp-ack", name="n2")
    link(db_session, "fp-ack", acknowledged)

    quiet = seed_incident(db_session, "quiet")
    seed_alert(db_session, "fp-quiet", name="n2", status="resolved")
    link(db_session, "fp-quiet", quiet)

    seed_alert(db_session, "fp-no-incident", name="n2")


def test_forty_alerts_in_one_incident_count_once(db_session):
    incident = seed_incident(db_session, "busy")
    for index in range(40):
        seed_alert(db_session, f"fp-{index}")
        link(db_session, f"fp-{index}", incident)

    assert count(group_by="incident") == 1


def test_an_alert_in_two_incidents_counts_both(db_session):
    first = seed_incident(db_session, "first")
    second = seed_incident(db_session, "second")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", first)
    link(db_session, "fp-1", second)

    assert count(group_by="incident") == 2


@pytest.mark.parametrize(
    "incident_status, expected",
    [(None, 7), ("active", 7), ("firing", 6), ("acknowledged", 1)],
)
def test_incident_status_modes(graph, incident_status, expected):
    query = {"group_by": "incident"}
    if incident_status:
        query["incident_status"] = incident_status

    assert count(**query) == expected


def test_a_resolved_incident_on_a_shared_fingerprint_is_not_counted(db_session):
    resolved = seed_incident(db_session, "resolved", IncidentStatus.RESOLVED)
    firing = seed_incident(db_session, "firing")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", resolved)
    link(db_session, "fp-1", firing)

    assert count(group_by="incident", incident_status="firing") == 1
    assert count(group_by="incident", incident_status="active") == 1


def test_a_soft_deleted_link_is_not_counted(db_session):
    incident = seed_incident(db_session, "unlinked")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", incident, deleted_at=datetime(2025, 1, 1))

    assert count(group_by="incident") == 0


def test_incident_status_follows_the_enrichment_override(db_session):
    resolved_by_override = seed_incident(
        db_session,
        "resolved-by-override",
        IncidentStatus.FIRING,
        enrichment_status=IncidentStatus.RESOLVED,
    )
    firing_by_override = seed_incident(
        db_session,
        "firing-by-override",
        IncidentStatus.RESOLVED,
        enrichment_status=IncidentStatus.FIRING,
    )
    seed_alert(db_session, "fp-1")
    seed_alert(db_session, "fp-2")
    link(db_session, "fp-1", resolved_by_override)
    link(db_session, "fp-2", firing_by_override)

    assert count(group_by="incident", incident_status="firing") == 1


@pytest.mark.parametrize(
    "incident_status, seeded_status",
    [
        ("active", IncidentStatus.FIRING),
        ("firing", IncidentStatus.FIRING),
        ("acknowledged", IncidentStatus.ACKNOWLEDGED),
    ],
)
def test_a_hidden_incident_is_not_counted(db_session, incident_status, seeded_status):
    hidden = seed_incident(db_session, "hidden", seeded_status, is_visible=False)
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", hidden)

    assert count(group_by="incident", incident_status=incident_status) == 0


@pytest.mark.parametrize(
    "incident_status, seeded_status",
    [
        ("active", IncidentStatus.FIRING),
        ("firing", IncidentStatus.FIRING),
        ("acknowledged", IncidentStatus.ACKNOWLEDGED),
    ],
)
def test_a_candidate_incident_is_not_counted(
    db_session, incident_status, seeded_status
):
    candidate = seed_incident(db_session, "candidate", seeded_status, is_candidate=True)
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", candidate)

    assert count(group_by="incident", incident_status=incident_status) == 0


def test_a_visible_incident_is_still_counted_beside_hidden_and_candidate_ones(
    db_session,
):
    visible = seed_incident(db_session, "visible")
    hidden = seed_incident(db_session, "hidden", is_visible=False)
    candidate = seed_incident(db_session, "candidate", is_candidate=True)
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", visible)
    link(db_session, "fp-1", hidden)
    link(db_session, "fp-1", candidate)

    assert count(group_by="incident") == 1
    assert count(group_by="incident", incident_status="firing") == 1


def test_alerts_without_an_incident_contribute_nothing(db_session):
    seed_alert(db_session, "fp-1")

    assert count(group_by="incident") == 0


def test_cel_restricts_which_alerts_are_considered(graph):
    assert count(group_by="incident", cel="service == 's1'") == 1
    assert count(group_by="incident", cel="status == 'firing'") == 6
    assert count(group_by="incident", cel="status == 'resolved'") == 1


def test_cel_may_reference_incident_fields_while_grouped_by_incident(graph):
    assert count(group_by="incident", cel="incident.name == 'busy'") == 1
    assert count(group_by="incident", cel="incident.name == 'old-resolved'") == 0


@pytest.mark.parametrize(
    "group_by, expected",
    [
        ("name", 2),
        ("service", 2),
        ("assignee", 2),
        ("node_name", 2),
        ("application", 1),
        ("site", 1),
    ],
)
def test_distinct_values_ignore_nulls(graph, group_by, expected):
    assert count(group_by=group_by) == expected


def test_nothing_to_count_is_zero(db_session):
    assert count(group_by="incident") == 0
    assert count(group_by="service") == 0


def test_other_tenants_are_not_counted(graph, db_session):
    foreign = seed_incident(db_session, "foreign", tenant_id=OTHER_TENANT)
    seed_alert(
        db_session, "fp-foreign", service="s9", tenant_id=OTHER_TENANT
    )
    link(db_session, "fp-foreign", foreign, tenant_id=OTHER_TENANT)

    assert count(group_by="incident") == 7
    assert count(group_by="service") == 2
    assert count(tenant_id=OTHER_TENANT, group_by="incident") == 1
    assert count(tenant_id=OTHER_TENANT, group_by="service") == 1


def test_a_database_failure_is_raised_not_hidden_as_zero(db_session):
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with patch(
        "src.repositories.alerts.build_distinct_count_query", side_effect=failure
    ):
        with pytest.raises(OperationalError):
            count(group_by="incident")
