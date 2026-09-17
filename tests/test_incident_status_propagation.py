"""An incident's status reaches its linked alerts — but only on some transitions.

The rules, from `IncidentBl._status_to_propagate`:

  -> suppressed / acknowledged : always propagates.
  -> firing, from suppressed or acknowledged : propagates (undoes the above).
  -> firing, from firing : nothing.
  -> resolved : never. Resolving an incident says something about the incident,
     not about whether each underlying alert stopped firing.

A resolved alert is never touched by any of it: it has finished its own
lifecycle, and dragging it back out would misreport reality.

Suppression travels as dismiss state, never as status='suppressed'. Alerts derive
suppression from dismiss_mode/dismissed_until, so copying the incident's deadline
is what makes an alert come back on the same clock — writing a status would strand
it suppressed after the deadline passed.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import Alert, LastAlert, LastAlertToIncident
from src.models.db.incident import Incident, IncidentDismissMode, IncidentStatus
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.incidents_bl import IncidentBl

ACTOR = AuthenticatedEntity(tenant_id=SINGLE_TENANT_UUID, email="tester@keep")


def _future(minutes=60):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _past(minutes=60):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _incident(db_session, status=IncidentStatus.FIRING, **kwargs) -> Incident:
    incident = Incident(
        tenant_id=SINGLE_TENANT_UUID,
        user_generated_name="prop",
        user_summary="s",
        generated_summary="s",
        status=status.value,
        **kwargs,
    )
    db_session.add(incident)
    db_session.commit()
    db_session.refresh(incident)
    return incident


def _alert(db_session, fingerprint, provider_status="firing", **last_alert_kwargs):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    alert = Alert(
        id=uuid.uuid4(),
        tenant_id=SINGLE_TENANT_UUID,
        timestamp=now,
        provider_type="test",
        provider_id="t1",
        fingerprint=fingerprint,
        name=fingerprint,
        severity="critical",
        status=provider_status,
    )
    db_session.add(alert)
    db_session.flush()
    db_session.add(
        LastAlert(
            tenant_id=SINGLE_TENANT_UUID,
            fingerprint=fingerprint,
            alert_id=alert.id,
            timestamp=now,
            first_timestamp=now,
            last_received=now,
            **last_alert_kwargs,
        )
    )
    db_session.commit()
    return fingerprint


def _link(db_session, incident, fingerprint):
    db_session.add(
        LastAlertToIncident(
            tenant_id=SINGLE_TENANT_UUID,
            incident_id=incident.id,
            fingerprint=fingerprint,
        )
    )
    db_session.commit()


def _state(db_session, fingerprint) -> dict:
    db_session.expire_all()
    last_alert = (
        db_session.query(LastAlert)
        .filter(LastAlert.fingerprint == fingerprint)
        .one()
    )
    provider_status = (
        db_session.query(Alert.status)
        .filter(Alert.fingerprint == fingerprint)
        .scalar()
    )
    return {
        "effective": last_alert.get_effective_status(provider_status),
        "override": last_alert.status,
        "dismiss_mode": last_alert.dismiss_mode,
        "dismissed_until": last_alert.dismissed_until,
        "assignee": last_alert.assignee,
    }


def _bl(db_session) -> IncidentBl:
    return IncidentBl(SINGLE_TENANT_UUID, db_session, user=ACTOR.email)


# === the transition matrix, decided without touching the database ===


@pytest.mark.parametrize(
    "previous,new_status,expected",
    [
        # Entering suppressed/acknowledged always propagates.
        ("firing", IncidentStatus.SUPPRESSED, IncidentStatus.SUPPRESSED),
        ("firing", IncidentStatus.ACKNOWLEDGED, IncidentStatus.ACKNOWLEDGED),
        ("acknowledged", IncidentStatus.SUPPRESSED, IncidentStatus.SUPPRESSED),
        ("suppressed", IncidentStatus.ACKNOWLEDGED, IncidentStatus.ACKNOWLEDGED),
        ("resolved", IncidentStatus.ACKNOWLEDGED, IncidentStatus.ACKNOWLEDGED),
        # Leaving suppressed or acknowledged for firing undoes the propagation.
        ("suppressed", IncidentStatus.FIRING, IncidentStatus.FIRING),
        ("acknowledged", IncidentStatus.FIRING, IncidentStatus.FIRING),
        # Nothing to undo.
        ("firing", IncidentStatus.FIRING, None),
        ("resolved", IncidentStatus.FIRING, None),
        # Resolving an incident never reaches its alerts.
        ("firing", IncidentStatus.RESOLVED, None),
        ("suppressed", IncidentStatus.RESOLVED, None),
        ("acknowledged", IncidentStatus.RESOLVED, None),
    ],
)
def test_status_to_propagate(previous, new_status, expected):
    assert IncidentBl._status_to_propagate(previous, new_status) is expected


# === acknowledged ===


@pytest.mark.asyncio
async def test_acknowledge_propagates_to_non_resolved_alerts(db_session):
    incident = _incident(db_session)
    firing = _alert(db_session, "prop-ack-firing")
    resolved = _alert(db_session, "prop-ack-resolved", provider_status="resolved")
    for fp in (firing, resolved):
        _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id, IncidentStatus.ACKNOWLEDGED, ACTOR
    )

    assert _state(db_session, firing)["effective"] == "acknowledged"
    # Untouched: a resolved alert has finished its own lifecycle.
    assert _state(db_session, resolved)["effective"] == "resolved"
    assert _state(db_session, resolved)["override"] is None


@pytest.mark.asyncio
async def test_acknowledge_clears_an_alerts_existing_dismissal(db_session):
    incident = _incident(db_session)
    fp = _alert(
        db_session,
        "prop-ack-dismissed",
        dismiss_mode=IncidentDismissMode.PERMANENT.value,
    )
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id, IncidentStatus.ACKNOWLEDGED, ACTOR
    )

    state = _state(db_session, fp)
    assert state["effective"] == "acknowledged"
    assert state["dismiss_mode"] is None


# === suppressed ===


@pytest.mark.asyncio
async def test_suppress_propagates_as_dismiss_state_not_status(db_session):
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-sup-firing")
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id, IncidentStatus.SUPPRESSED, ACTOR
    )

    state = _state(db_session, fp)
    assert state["effective"] == "suppressed"
    assert state["dismiss_mode"] == IncidentDismissMode.PERMANENT.value
    # Crucially NOT stored as a status — that is what lets it expire.
    assert state["override"] is None


@pytest.mark.asyncio
async def test_suppress_until_copies_the_deadline_to_the_alerts(db_session):
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-sup-until")
    _link(db_session, incident, fp)
    deadline = _future(45)

    await _bl(db_session).change_status(
        incident.id,
        IncidentStatus.SUPPRESSED,
        ACTOR,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
        dismissed_until=deadline,
    )

    state = _state(db_session, fp)
    assert state["effective"] == "suppressed"
    assert state["dismiss_mode"] == IncidentDismissMode.DISMISS_UNTIL.value
    assert state["dismissed_until"] is not None


@pytest.mark.asyncio
async def test_an_expired_propagated_dismissal_reverts_the_alert(db_session):
    """Incident and alert come back on the same clock, with no write."""
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-sup-expired")
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id,
        IncidentStatus.SUPPRESSED,
        ACTOR,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
        dismissed_until=_future(45),
    )
    assert _state(db_session, fp)["effective"] == "suppressed"

    # Simulate the deadline passing on both sides.
    db_session.expire_all()
    incident = db_session.get(Incident, incident.id)
    incident.dismissed_until = _past(5)
    last_alert = (
        db_session.query(LastAlert).filter(LastAlert.fingerprint == fp).one()
    )
    last_alert.dismissed_until = _past(5)
    db_session.commit()

    assert incident.get_effective_status() == "firing"
    assert _state(db_session, fp)["effective"] == "firing"


@pytest.mark.asyncio
async def test_suppress_preserves_the_status_the_alert_reverts_to(db_session):
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-sup-revert", status="acknowledged")
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id,
        IncidentStatus.SUPPRESSED,
        ACTOR,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
        dismissed_until=_future(45),
    )
    assert _state(db_session, fp)["effective"] == "suppressed"
    # The override survives underneath, so expiry returns it to acknowledged
    # rather than to the provider's firing.
    assert _state(db_session, fp)["override"] == "acknowledged"


# === firing ===


@pytest.mark.asyncio
async def test_un_suppressing_returns_alerts_to_firing(db_session):
    incident = _incident(db_session)
    firing = _alert(db_session, "prop-unsup-a")
    resolved = _alert(db_session, "prop-unsup-b", provider_status="resolved")
    for fp in (firing, resolved):
        _link(db_session, incident, fp)
    bl = _bl(db_session)

    await bl.change_status(incident.id, IncidentStatus.SUPPRESSED, ACTOR)
    assert _state(db_session, firing)["effective"] == "suppressed"

    await bl.change_status(incident.id, IncidentStatus.FIRING, ACTOR)

    state = _state(db_session, firing)
    assert state["effective"] == "firing"
    assert state["dismiss_mode"] is None
    # Still resolved, never dragged back out.
    assert _state(db_session, resolved)["effective"] == "resolved"


@pytest.mark.asyncio
async def test_un_acknowledging_returns_alerts_to_firing(db_session):
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-unack")
    _link(db_session, incident, fp)
    bl = _bl(db_session)

    await bl.change_status(incident.id, IncidentStatus.ACKNOWLEDGED, ACTOR)
    assert _state(db_session, fp)["effective"] == "acknowledged"

    await bl.change_status(incident.id, IncidentStatus.FIRING, ACTOR)
    assert _state(db_session, fp)["effective"] == "firing"


# === resolved: the one status that stays put ===


@pytest.mark.asyncio
async def test_resolving_an_incident_leaves_its_alerts_alone(db_session):
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-res-firing")
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(incident.id, IncidentStatus.RESOLVED, ACTOR)

    state = _state(db_session, fp)
    assert state["effective"] == "firing"
    assert state["override"] is None


@pytest.mark.asyncio
async def test_resolving_does_not_lift_an_existing_suppression(db_session):
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-res-sup")
    _link(db_session, incident, fp)
    bl = _bl(db_session)

    await bl.change_status(incident.id, IncidentStatus.SUPPRESSED, ACTOR)
    await bl.change_status(incident.id, IncidentStatus.RESOLVED, ACTOR)

    # The incident resolved; the alert keeps the dismissal it was given.
    assert _state(db_session, fp)["effective"] == "suppressed"


# === newly linked alerts inherit ===


@pytest.mark.asyncio
async def test_a_new_alert_inherits_a_suppressed_incidents_dismissal(db_session):
    incident = _incident(db_session)
    bl = _bl(db_session)
    await bl.change_status(
        incident.id,
        IncidentStatus.SUPPRESSED,
        ACTOR,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
        dismissed_until=_future(90),
    )

    late = _alert(db_session, "prop-late-sup")
    await bl.add_alerts_to_incident(incident.id, [late], change_by=ACTOR)

    state = _state(db_session, late)
    assert state["effective"] == "suppressed"
    assert state["dismiss_mode"] == IncidentDismissMode.DISMISS_UNTIL.value
    # Same deadline as the incident, so they come back together.
    assert state["dismissed_until"] is not None


@pytest.mark.asyncio
async def test_a_new_alert_inherits_an_acknowledged_incident(db_session):
    incident = _incident(db_session)
    bl = _bl(db_session)
    await bl.change_status(incident.id, IncidentStatus.ACKNOWLEDGED, ACTOR)

    late = _alert(db_session, "prop-late-ack")
    await bl.add_alerts_to_incident(incident.id, [late], change_by=ACTOR)

    assert _state(db_session, late)["effective"] == "acknowledged"


@pytest.mark.asyncio
async def test_a_new_resolved_alert_is_not_dragged_out_of_resolved(db_session):
    incident = _incident(db_session)
    bl = _bl(db_session)
    await bl.change_status(incident.id, IncidentStatus.SUPPRESSED, ACTOR)

    late = _alert(db_session, "prop-late-res", provider_status="resolved")
    await bl.add_alerts_to_incident(incident.id, [late], change_by=ACTOR)

    assert _state(db_session, late)["effective"] == "resolved"


@pytest.mark.asyncio
async def test_a_new_alert_on_a_firing_incident_inherits_nothing(db_session):
    incident = _incident(db_session)
    late = _alert(db_session, "prop-late-firing")
    await _bl(db_session).add_alerts_to_incident(
        incident.id, [late], change_by=ACTOR
    )

    state = _state(db_session, late)
    assert state["effective"] == "firing"
    assert state["dismiss_mode"] is None


@pytest.mark.asyncio
async def test_a_new_alert_on_a_resolved_incident_inherits_nothing(db_session):
    incident = _incident(db_session, status=IncidentStatus.RESOLVED)
    late = _alert(db_session, "prop-late-on-resolved")
    await _bl(db_session).add_alerts_to_incident(
        incident.id, [late], change_by=ACTOR
    )

    assert _state(db_session, late)["effective"] == "firing"


# === assignee ===


@pytest.mark.asyncio
async def test_self_assign_acknowledges_and_assigns_alerts(db_session):
    incident = _incident(db_session)
    firing = _alert(db_session, "prop-assign-firing")
    resolved = _alert(db_session, "prop-assign-resolved", provider_status="resolved")
    for fp in (firing, resolved):
        _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id, IncidentStatus.ACKNOWLEDGED, ACTOR
    )

    db_session.expire_all()
    incident = db_session.get(Incident, incident.id)
    assert incident.get_effective_status() == "acknowledged"
    assert incident.assignee == ACTOR.email

    state = _state(db_session, firing)
    assert state["effective"] == "acknowledged"
    assert state["assignee"] == ACTOR.email

    # A resolved alert is not claimed either — it is nobody's work any more.
    assert _state(db_session, resolved)["assignee"] is None


@pytest.mark.asyncio
async def test_acknowledging_via_status_also_assigns_the_alerts(db_session):
    """Acknowledging is claiming, whichever endpoint it arrives through."""
    incident = _incident(db_session)
    fp = _alert(db_session, "prop-ackassign")
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(
        incident.id, IncidentStatus.ACKNOWLEDGED, ACTOR
    )

    assert _state(db_session, fp)["assignee"] == ACTOR.email


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new_status", [IncidentStatus.SUPPRESSED, IncidentStatus.FIRING]
)
async def test_other_status_changes_do_not_assign_alerts(db_session, new_status):
    # Only acknowledging claims an alert; suppressing or re-firing says nothing
    # about who owns it.
    incident = _incident(db_session)
    fp = _alert(db_session, f"prop-noassign-{new_status.value}")
    _link(db_session, incident, fp)

    await _bl(db_session).change_status(incident.id, new_status, ACTOR)

    assert _state(db_session, fp)["assignee"] is None


# === no alerts linked ===


@pytest.mark.asyncio
async def test_propagation_is_a_no_op_with_no_linked_alerts(db_session):
    incident = _incident(db_session)
    dto = await _bl(db_session).change_status(
        incident.id, IncidentStatus.SUPPRESSED, ACTOR
    )
    assert dto.status == IncidentStatus.SUPPRESSED
