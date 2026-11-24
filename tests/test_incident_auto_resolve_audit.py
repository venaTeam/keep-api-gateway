"""Resolving the last open alert of an incident resolves the incident, on record.

A user who resolves alerts can thereby resolve an incident they never touched, so
the incident's activity says who did it and how. Nothing is written when nobody
acted (provider path) or nothing changed.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from src.models.action_type import ActionType
from src.models.alert import AlertStatus
from src.models.db.alert import AlertAudit, LastAlertToIncident
from src.models.db.incident import Incident, IncidentStatus
from src.models.db.rule import ResolveOn
from src.models.db.tenant import Tenant
from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.services.enrichments_bl import EnrichmentsBl
from tests.fixtures.client import client, setup_api_key, test_app  # noqa
from tests.fixtures.identity import bearer_headers

OTHER_TENANT = "other-tenant"
EMAIL = "noc@example.com"

single_tenant = pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)


@pytest.fixture
def make_incident(db_session, create_alert):
    """An incident over alerts (firing unless stated otherwise)."""

    def _make(
        fingerprints,
        *,
        tenant_id=GENERIC_TENANT_UUID,
        resolve_on=ResolveOn.ALL,
        status=IncidentStatus.FIRING,
        alert_status=AlertStatus.FIRING,
        create=True,
    ) -> Incident:
        if create:
            for fingerprint in fingerprints:
                create_alert(
                    fingerprint,
                    alert_status,
                    datetime.utcnow(),
                    {"name": fingerprint},
                    tenant_id=tenant_id,
                )
        incident = Incident(
            tenant_id=tenant_id,
            user_generated_name="incident",
            user_summary="",
            generated_summary="",
            alerts_count=len(fingerprints),
            resolve_on=resolve_on.value,
            status=status.value,
        )
        db_session.add(incident)
        db_session.commit()
        for fingerprint in fingerprints:
            db_session.add(
                LastAlertToIncident(
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    incident_id=incident.id,
                )
            )
        db_session.commit()
        return incident

    return _make


def _status(db_session, incident):
    db_session.expire_all()
    return db_session.get(Incident, incident.id).status


def _incident_audits(db_session):
    db_session.expire_all()
    return (
        db_session.query(AlertAudit)
        .filter(AlertAudit.action == ActionType.INCIDENT_STATUS_CHANGE.value)
        .all()
    )


def _resolve_batch(client, fingerprints, status="resolved", source=None):
    headers = bearer_headers("noc")
    if source:
        headers["X-Keep-Source"] = source
    response = client.post(
        "/alerts/batch_enrich",
        headers=headers,
        json={"fingerprints": fingerprints, "enrichments": {"status": status}},
    )
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def _resolve_single(client, fingerprint, source=None):
    headers = bearer_headers("noc")
    if source:
        headers["X-Keep-Source"] = source
    response = client.post(
        "/alerts/enrich",
        headers=headers,
        json={"fingerprint": fingerprint, "enrichments": {"status": "resolved"}},
    )
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


# --------------------------------------------------------------------------- #
# One row per incident that really changed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source,marker",
    [(None, ""), ("ui", ""), ("assistant", " via Keep Assistant")],
)
@single_tenant
def test_batch_writes_one_row_per_incident(
    db_session, client, test_app, make_incident, source, marker
):
    incident = make_incident(["a", "b", "c"])

    _resolve_batch(client, ["a", "b", "c"], source=source)

    assert _status(db_session, incident) == "resolved"
    (audit,) = _incident_audits(db_session)
    assert audit.tenant_id == GENERIC_TENANT_UUID
    assert audit.fingerprint == str(incident.id)
    assert audit.user_id == EMAIL
    assert audit.description == (
        "Incident status changed from firing to resolved because all its alerts "
        f"were resolved by {EMAIL}{marker}"
    )


@single_tenant
def test_single_enrich_writes_one_row(db_session, client, test_app, make_incident):
    incident = make_incident(["a"], status=IncidentStatus.ACKNOWLEDGED)

    # The header marks batch_enrich only: Keep Assistant never calls this route.
    _resolve_single(client, "a", source="assistant")

    assert _status(db_session, incident) == "resolved"
    (audit,) = _incident_audits(db_session)
    assert (audit.fingerprint, audit.user_id) == (str(incident.id), EMAIL)
    assert audit.description == (
        "Incident status changed from acknowledged to resolved because all its "
        f"alerts were resolved by {EMAIL}"
    )


@single_tenant
def test_alerts_shared_by_incidents(db_session, client, test_app, make_incident):
    first = make_incident(["a", "b"])
    second = make_incident(["a", "b"], create=False)

    _resolve_batch(client, ["a", "b"])

    assert _status(db_session, first) == _status(db_session, second) == "resolved"
    assert sorted(a.fingerprint for a in _incident_audits(db_session)) == sorted(
        [str(first.id), str(second.id)]
    )


@single_tenant
def test_resolving_again_adds_nothing(db_session, client, test_app, make_incident):
    make_incident(["a"])

    _resolve_batch(client, ["a"])
    _resolve_batch(client, ["a"])
    _resolve_single(client, "a")

    assert len(_incident_audits(db_session)) == 1


@single_tenant
def test_the_alert_that_completes_the_set_gets_the_row(
    db_session, client, test_app, make_incident
):
    incident = make_incident(["a", "b"])

    _resolve_batch(client, ["a"])
    assert _status(db_session, incident) == "firing"
    assert _incident_audits(db_session) == []

    _resolve_single(client, "b")
    assert _status(db_session, incident) == "resolved"
    assert len(_incident_audits(db_session)) == 1


@single_tenant
def test_alert_resolved_by_its_provider_counts_as_resolved(
    db_session, client, test_app, make_incident, create_alert
):
    """Effective status: the user's value, else what the provider sent."""
    create_alert("b", AlertStatus.RESOLVED, datetime.utcnow(), {"name": "b"})
    create_alert("a", AlertStatus.FIRING, datetime.utcnow(), {"name": "a"})
    incident = make_incident(["a", "b"], create=False)

    _resolve_batch(client, ["a"])

    assert _status(db_session, incident) == "resolved"
    assert len(_incident_audits(db_session)) == 1


@single_tenant
def test_alerts_removed_from_the_incident_do_not_hold_it_open(
    db_session, client, test_app, make_incident
):
    incident = make_incident(["a", "removed"])
    link = db_session.query(LastAlertToIncident).filter_by(fingerprint="removed").one()
    link.deleted_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.add(link)
    db_session.commit()

    _resolve_batch(client, ["a"])

    assert _status(db_session, incident) == "resolved"


# --------------------------------------------------------------------------- #
# Nothing changed, nothing written
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "resolve_on", [ResolveOn.FIRST, ResolveOn.LAST, ResolveOn.NEVER]
)
@single_tenant
def test_other_resolve_modes_are_left_alone(
    db_session, client, test_app, make_incident, resolve_on
):
    incident = make_incident(["a"], resolve_on=resolve_on)

    _resolve_batch(client, ["a"])

    assert _status(db_session, incident) == "firing"
    assert _incident_audits(db_session) == []


@single_tenant
def test_one_open_alert_keeps_the_incident_open(
    db_session, client, test_app, make_incident
):
    incident = make_incident(["a", "b", "open"])

    _resolve_batch(client, ["a", "b"])

    assert _status(db_session, incident) == "firing"
    assert _incident_audits(db_session) == []


@single_tenant
def test_already_resolved_incident_gets_no_row(
    db_session, client, test_app, make_incident
):
    make_incident(["a"], status=IncidentStatus.RESOLVED)

    _resolve_batch(client, ["a"])

    assert _incident_audits(db_session) == []


@pytest.mark.parametrize("status", ["acknowledged", "suppressed", "firing"])
@single_tenant
def test_other_status_changes_resolve_nothing(
    db_session, client, test_app, make_incident, status
):
    incident = make_incident(["a"])

    _resolve_batch(client, ["a"], status=status)

    assert _status(db_session, incident) == "firing"
    assert _incident_audits(db_session) == []


@single_tenant
def test_other_tenant_is_untouched(db_session, client, test_app, make_incident):
    db_session.add(Tenant(id=OTHER_TENANT, name="other", created_by="t@example.com"))
    db_session.commit()
    ours = make_incident(["a"])
    theirs = make_incident(
        ["a"], tenant_id=OTHER_TENANT, alert_status=AlertStatus.RESOLVED
    )

    _resolve_batch(client, ["a"])

    assert _status(db_session, ours) == "resolved"
    assert _status(db_session, theirs) == "firing"
    (audit,) = _incident_audits(db_session)
    assert (audit.tenant_id, audit.fingerprint) == (GENERIC_TENANT_UUID, str(ours.id))


def test_incidents_of_another_tenant_are_ignored(db_session, make_incident):
    """Even when handed in, with every alert resolved."""
    db_session.add(Tenant(id=OTHER_TENANT, name="other", created_by="t@example.com"))
    db_session.commit()
    theirs = make_incident(
        ["a"], tenant_id=OTHER_TENANT, alert_status=AlertStatus.RESOLVED
    )

    EnrichmentsBl(
        GENERIC_TENANT_UUID, db=db_session
    ).resolve_incidents_with_all_alerts_resolved([theirs], resolved_by=EMAIL)

    assert _status(db_session, theirs) == "firing"
    assert _incident_audits(db_session) == []


def test_provider_path_resolves_without_an_audit_row(db_session, make_incident):
    """providers/base/base_provider.py passes no actor."""
    incident = make_incident(["a"], alert_status=AlertStatus.RESOLVED)

    EnrichmentsBl(GENERIC_TENANT_UUID, db=db_session).check_incident_resolution(
        SimpleNamespace(fingerprint="a")
    )

    assert _status(db_session, incident) == "resolved"
    assert _incident_audits(db_session) == []


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("count", [1, 5])
def test_one_select_whatever_the_number_of_incidents(
    db_session, make_incident, monkeypatch, count
):
    from src.repositories import db as db_repository

    incidents = [
        make_incident([f"fp-{i}"], alert_status=AlertStatus.RESOLVED)
        for i in range(count)
    ]
    for incident in incidents:
        db_session.refresh(incident)
    bl = EnrichmentsBl(GENERIC_TENANT_UUID, db=db_session)

    selects, sessions = [], []
    original_session = db_repository.Session

    def counting_session(*args, **kwargs):
        sessions.append(1)
        return original_session(*args, **kwargs)

    monkeypatch.setattr(db_repository, "Session", counting_session)

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append((statement, parameters))

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        # Repeated incidents (one per alert) must not cost or write more.
        bl.resolve_incidents_with_all_alerts_resolved(
            incidents + incidents, resolved_by=EMAIL
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(selects) == 1, selects
    assert GENERIC_TENANT_UUID in str(selects[0][1])
    assert sessions == []
    assert len(_incident_audits(db_session)) == count


def test_no_query_without_a_candidate(db_session, make_incident):
    incident = make_incident(["a"], resolve_on=ResolveOn.NEVER)
    db_session.refresh(incident)
    bl = EnrichmentsBl(GENERIC_TENANT_UUID, db=db_session)

    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        bl.resolve_incidents_with_all_alerts_resolved([incident], resolved_by=EMAIL)
        bl.resolve_incidents_with_all_alerts_resolved([], resolved_by=EMAIL)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements == []
