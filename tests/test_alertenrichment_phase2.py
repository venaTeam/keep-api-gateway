"""Phase 2 (alertenrichment removal) — db-layer behavior tests.

Covers the canonical shared-logic spec for keep-api-gateway:
  - dismissed -> dismiss_mode translation (normalize_enrichments)
  - unknown enrichment key handling: strict=True raises (route -> 422),
    strict=False discards with a warning
  - note-preservation guard
  - status_disposable: cleared on a non-resolved re-fire when TRUE
  - dismiss survive-resolve: permanent / dismiss_until survive, until_resolved clears
  - D1: enrich-before-first-alert -> no column write, AlertAudit still created
  - deleted typed column set/clear
  - DTO build sources user state + tracking from LastAlert
"""

from datetime import datetime, timezone

import pytest

from src.models.action_type import ActionType
from src.models.alert import AlertStatus
from src.models.db.alert import Alert, AlertAudit, AlertEnrichment, LastAlert
from src.repositories.db import (
    enrich_entity,
    get_last_alert_by_fingerprint,
    normalize_enrichments,
    set_last_alert,
)
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts


def _make_alert(db_session, fingerprint, status="firing", ts=None):
    ts = ts or datetime.now(timezone.utc)
    alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="test",
        provider_id="test",
        fingerprint=fingerprint,
        source="test",
        status=status,
        severity="critical",
        name=f"name-{fingerprint}",
        timestamp=ts,
    )
    db_session.add(alert)
    db_session.commit()
    return alert


def _make_last_alert(db_session, alert, **cols):
    la = LastAlert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint=alert.fingerprint,
        timestamp=alert.timestamp,
        first_timestamp=alert.timestamp,
        alert_id=alert.id,
        last_received=alert.timestamp,
        **cols,
    )
    db_session.add(la)
    db_session.commit()
    return la


def _audit_count(db_session, fingerprint):
    return (
        db_session.query(AlertAudit)
        .filter(
            AlertAudit.tenant_id == SINGLE_TENANT_UUID,
            AlertAudit.fingerprint == fingerprint,
        )
        .count()
    )


# --------------------------------------------------------------------------- #
# normalize_enrichments: translation + unknown-key handling
# --------------------------------------------------------------------------- #
def test_normalize_dismissed_true_permanent():
    out = normalize_enrichments({"dismissed": True})
    assert out["status"] == "suppressed"
    assert out["dismiss_mode"] == "permanent"


def test_normalize_dismissed_true_with_until():
    out = normalize_enrichments({"dismissed": True, "dismissed_until": "2026-01-01T00:00:00Z"})
    assert out["status"] == "suppressed"
    assert out["dismiss_mode"] == "dismiss_until"
    assert out["dismissed_until"] == "2026-01-01T00:00:00Z"


def test_normalize_dismissed_false_clears():
    out = normalize_enrichments({"dismissed": False})
    assert out["status"] is None
    assert out["dismiss_mode"] is None
    assert out["dismissed_until"] is None


def test_normalize_unknown_key_strict_raises():
    with pytest.raises(ValueError):
        normalize_enrichments({"ticket_url": "https://x"}, strict=True)


def test_normalize_unknown_key_nonstrict_discards():
    out = normalize_enrichments({"ticket_url": "https://x", "status": "acknowledged"}, strict=False)
    assert "ticket_url" not in out
    assert out["status"] == "acknowledged"


# --------------------------------------------------------------------------- #
# _enrich_entity: typed column write, note-guard, D1, AlertAudit
# --------------------------------------------------------------------------- #
def test_enrich_writes_typed_columns(db_session):
    alert = _make_alert(db_session, "fp-typed")
    _make_last_alert(db_session, alert)

    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-typed",
        {"status": "acknowledged", "assignee": "bob", "note": "hi"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
    )
    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-typed", db_session)
    assert la.status == "acknowledged"
    assert la.assignee == "bob"
    assert la.note == "hi"
    assert _audit_count(db_session, "fp-typed") == 1


def test_enrich_unknown_key_raises_strict(db_session):
    alert = _make_alert(db_session, "fp-unknown")
    _make_last_alert(db_session, alert)
    with pytest.raises(ValueError):
        enrich_entity(
            SINGLE_TENANT_UUID,
            "fp-unknown",
            {"ticket_url": "https://x"},
            action_type=ActionType.GENERIC_ENRICH,
            action_callee="bob@x",
            action_description="t",
            session=db_session,
        )


def test_note_guard_preserves_existing_note(db_session):
    alert = _make_alert(db_session, "fp-note")
    _make_last_alert(db_session, alert, note="keepme")

    # empty incoming note must NOT erase the existing note
    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-note",
        {"status": "acknowledged", "note": "   "},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
    )
    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-note", db_session)
    assert la.note == "keepme"
    assert la.status == "acknowledged"


def test_note_guard_force_clears_note(db_session):
    alert = _make_alert(db_session, "fp-noteforce")
    _make_last_alert(db_session, alert, note="keepme")

    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-noteforce",
        {"note": None},
        action_type=ActionType.UNCOMMENT,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        force=True,
    )
    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-noteforce", db_session)
    assert la.note is None


def test_d1_enrich_before_first_alert_no_column_write_but_audit(db_session):
    # No LastAlert row exists for this fingerprint
    result = enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-d1",
        {"status": "acknowledged"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
    )
    assert result is None  # D1: no LastAlert -> no write
    assert get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-d1", db_session) is None
    # AlertAudit row is still created
    assert _audit_count(db_session, "fp-d1") == 1


def test_deleted_set_and_clear(db_session):
    alert = _make_alert(db_session, "fp-del")
    _make_last_alert(db_session, alert)

    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-del",
        {"deleted": True},
        action_type=ActionType.DELETE_ALERT,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
    )
    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-del", db_session)
    assert la.deleted is True

    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-del",
        {"deleted": False},
        action_type=ActionType.DELETE_ALERT,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
    )
    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-del", db_session)
    assert la.deleted is False


# --------------------------------------------------------------------------- #
# set_last_alert: status_disposable + dismiss survive-resolve clearing
# --------------------------------------------------------------------------- #
def test_status_disposable_cleared_on_non_resolved_refire(db_session):
    alert = _make_alert(db_session, "fp-disp", status="firing")
    _make_last_alert(
        db_session, alert, status="acknowledged", status_disposable=True
    )

    newer = _make_alert(
        db_session,
        "fp-disp",
        status="firing",
        ts=datetime.now(timezone.utc),
    )
    set_last_alert(SINGLE_TENANT_UUID, newer, session=db_session)

    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-disp", db_session)
    assert la.status is None
    assert la.status_disposable is False


def test_status_disposable_false_persists_on_refire(db_session):
    alert = _make_alert(db_session, "fp-nondisp", status="firing")
    _make_last_alert(
        db_session, alert, status="acknowledged", status_disposable=False
    )

    newer = _make_alert(
        db_session, "fp-nondisp", status="firing", ts=datetime.now(timezone.utc)
    )
    set_last_alert(SINGLE_TENANT_UUID, newer, session=db_session)

    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-nondisp", db_session)
    assert la.status == "acknowledged"


def test_dismiss_until_resolved_clears_on_resolve(db_session):
    alert = _make_alert(db_session, "fp-untilres", status="firing")
    _make_last_alert(
        db_session, alert, status="suppressed", dismiss_mode="until_resolved"
    )

    resolved = _make_alert(
        db_session,
        "fp-untilres",
        status=AlertStatus.RESOLVED.value,
        ts=datetime.now(timezone.utc),
    )
    set_last_alert(SINGLE_TENANT_UUID, resolved, session=db_session)

    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-untilres", db_session)
    assert la.status is None
    assert la.dismiss_mode is None


@pytest.mark.parametrize("mode", ["permanent", "dismiss_until"])
def test_dismiss_survives_resolve(db_session, mode):
    fp = f"fp-survive-{mode}"
    alert = _make_alert(db_session, fp, status="firing")
    _make_last_alert(db_session, alert, status="suppressed", dismiss_mode=mode)

    resolved = _make_alert(
        db_session, fp, status=AlertStatus.RESOLVED.value, ts=datetime.now(timezone.utc)
    )
    set_last_alert(SINGLE_TENANT_UUID, resolved, session=db_session)

    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, fp, db_session)
    assert la.status == "suppressed"
    assert la.dismiss_mode == mode


def test_set_last_alert_writes_tracking(db_session):
    alert = _make_alert(db_session, "fp-track", status="firing")
    _make_last_alert(db_session, alert)

    newer = _make_alert(
        db_session, "fp-track", status="firing", ts=datetime.now(timezone.utc)
    )
    set_last_alert(
        SINGLE_TENANT_UUID,
        newer,
        session=db_session,
        tracking={"firing_counter": 3, "unresolved_counter": 2},
    )
    la = get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, "fp-track", db_session)
    assert la.firing_counter == 3
    assert la.unresolved_counter == 2


# --------------------------------------------------------------------------- #
# DTO build sources user-state + derived dismissed from LastAlert
# --------------------------------------------------------------------------- #
def test_dto_build_from_last_alert_columns(db_session):
    alert = _make_alert(db_session, "fp-dto", status="firing")
    _make_last_alert(
        db_session,
        alert,
        status="suppressed",
        dismiss_mode="permanent",
        assignee="alice",
        note="dto-note",
        firing_counter=5,
    )

    dtos = convert_db_alerts_to_dto_alerts([alert], session=db_session)
    assert len(dtos) == 1
    dto = dtos[0]
    assert dto.status == "suppressed"
    assert dto.dismissed is True  # derived from status == 'suppressed'
    assert dto.dismiss_mode == "permanent"
    assert dto.assignee == "alice"
    assert dto.note == "dto-note"
    assert dto.firing_counter == 5


# --------------------------------------------------------------------------- #
# AG-3b: INCIDENT enrichment stays on the legacy AlertEnrichment JSONB row
# (UUID-keyed). No LastAlert involvement, no translation, no D1 no-op, no
# strict unknown-key rejection — arbitrary keys are preserved verbatim.
# --------------------------------------------------------------------------- #
def _get_alertenrichment(db_session, fingerprint):
    return (
        db_session.query(AlertEnrichment)
        .filter(
            AlertEnrichment.tenant_id == SINGLE_TENANT_UUID,
            AlertEnrichment.alert_fingerprint == fingerprint,
        )
        .first()
    )


def test_incident_enrich_writes_alertenrichment_row(db_session):
    incident_id = "11111111-1111-1111-1111-111111111111"
    # No LastAlert exists for an incident id; the incident branch must NOT no-op.
    enrich_entity(
        SINGLE_TENANT_UUID,
        incident_id,
        {"ticket_url": "https://x", "severity": "high"},
        action_type=ActionType.INCIDENT_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        entity_type="incident",
    )

    enr = _get_alertenrichment(db_session, incident_id)
    assert enr is not None  # AlertEnrichment row written (no D1 no-op for incidents)
    # arbitrary keys preserved verbatim (no strict rejection, no translation)
    assert enr.enrichments["ticket_url"] == "https://x"
    assert enr.enrichments["severity"] == "high"
    # no LastAlert created for an incident id
    assert get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, incident_id, db_session) is None
    assert _audit_count(db_session, incident_id) == 1


def test_incident_enrich_merges_on_second_write(db_session):
    incident_id = "22222222-2222-2222-2222-222222222222"
    enrich_entity(
        SINGLE_TENANT_UUID,
        incident_id,
        {"foo": "1"},
        action_type=ActionType.INCIDENT_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        entity_type="incident",
    )
    enrich_entity(
        SINGLE_TENANT_UUID,
        incident_id,
        {"bar": "2"},
        action_type=ActionType.INCIDENT_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        entity_type="incident",
    )
    enr = _get_alertenrichment(db_session, incident_id)
    # merge (not replace) — both keys present
    assert enr.enrichments == {"foo": "1", "bar": "2"}


def test_incident_enrich_no_dismissed_translation(db_session):
    incident_id = "33333333-3333-3333-3333-333333333333"
    # the dismissed<->dismiss_mode translation must NOT be applied to incidents
    enrich_entity(
        SINGLE_TENANT_UUID,
        incident_id,
        {"dismissed": True},
        action_type=ActionType.INCIDENT_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        entity_type="incident",
    )
    enr = _get_alertenrichment(db_session, incident_id)
    assert enr.enrichments == {"dismissed": True}
    assert "dismiss_mode" not in enr.enrichments
    assert "status" not in enr.enrichments


def test_incident_unenrich_force_replaces(db_session):
    incident_id = "44444444-4444-4444-4444-444444444444"
    enrich_entity(
        SINGLE_TENANT_UUID,
        incident_id,
        {"foo": "1", "bar": "2"},
        action_type=ActionType.INCIDENT_ENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        entity_type="incident",
    )
    # force=True replaces the whole JSONB (how unenrich removes a key)
    enrich_entity(
        SINGLE_TENANT_UUID,
        incident_id,
        {"foo": "1"},
        action_type=ActionType.INCIDENT_UNENRICH,
        action_callee="bob@x",
        action_description="t",
        session=db_session,
        force=True,
        entity_type="incident",
    )
    enr = _get_alertenrichment(db_session, incident_id)
    assert enr.enrichments == {"foo": "1"}
