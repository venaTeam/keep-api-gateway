"""Phase 2 (alertenrichment removal) — route-level + DTO round-trip coverage.

The alerts routes (delete / assign / unenrich) run ``EnrichmentsBl.enrich_entity``
in-process and synchronously write the typed LastAlert columns, so a TestClient
call is observable directly in the DB. These tests assert the route -> typed
column contract that the UI relies on:

  - POST /alerts/unenrich clears the right columns per the route's
    ``clear_enrichments`` mapping (status/dismiss for ``dismissed``,
    ``note`` -> NULL, ``ticket_url`` -> NULL, ``deleted`` -> False) and 422s on an
    unknown key.
  - DELETE /alerts soft-deletes: {deleted: True, assignee: <user>}.
  - POST /alerts/{fp}/assign/{lr} sets {assignee, status=acknowledged}.

Plus a ``last_received`` round-trip: a datetime written via
``set_last_alert(tracking=...)`` must surface on the DTO as the canonical
``%Y-%m-%dT%H:%M:%S.%f``[:-3] + "Z" wire string.
"""

from datetime import datetime, timezone

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import LastAlert
from src.repositories.db import get_last_alert_by_fingerprint, set_last_alert
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
from tests.fixtures.client import client, setup_api_key, test_app  # noqa


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _set_last_alert_cols(db_session, fingerprint, **cols):
    """Apply typed columns onto an existing LastAlert row (seeded by create_alert)."""
    la = (
        db_session.query(LastAlert)
        .filter(
            LastAlert.tenant_id == SINGLE_TENANT_UUID,
            LastAlert.fingerprint == fingerprint,
        )
        .first()
    )
    assert la is not None, "create_alert should have seeded a LastAlert row"
    for k, v in cols.items():
        setattr(la, k, v)
    db_session.add(la)
    db_session.commit()
    return la


def _read_la(db_session, fingerprint):
    db_session.expire_all()  # drop any cached state; the BL committed via its own session
    return get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, fingerprint, db_session)


# --------------------------------------------------------------------------- #
# POST /alerts/unenrich — clear_enrichments mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_unenrich_dismissed_clears_status_and_dismiss(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("fp-un-dismiss", AlertStatus.FIRING, datetime.utcnow(), {"name": "a"})
    _set_last_alert_cols(
        db_session,
        "fp-un-dismiss",
        status="suppressed",
        dismiss_mode="permanent",
        dismissed_until=None,
    )

    resp = client.post(
        "/alerts/unenrich",
        headers={"x-api-key": "some-key"},
        json={"fingerprint": "fp-un-dismiss", "enrichments": ["dismissed"]},
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-un-dismiss")
    # `dismissed` maps to status/dismiss_mode/dismissed_until all NULL
    assert la.status is None
    assert la.dismiss_mode is None
    assert la.dismissed_until is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_unenrich_note_clears_note(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("fp-un-note", AlertStatus.FIRING, datetime.utcnow(), {"name": "a"})
    _set_last_alert_cols(db_session, "fp-un-note", note="keepme", status="acknowledged")

    resp = client.post(
        "/alerts/unenrich",
        headers={"x-api-key": "some-key"},
        json={"fingerprint": "fp-un-note", "enrichments": ["note"]},
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-un-note")
    # note -> NULL (force=True bypasses the note-guard); status untouched
    assert la.note is None
    assert la.status == "acknowledged"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_unenrich_ticket_url_clears_ticket(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("fp-un-ticket", AlertStatus.FIRING, datetime.utcnow(), {"name": "a"})
    _set_last_alert_cols(
        db_session, "fp-un-ticket", ticket_url="https://jira/x", ticket_type="jira"
    )

    resp = client.post(
        "/alerts/unenrich",
        headers={"x-api-key": "some-key"},
        json={"fingerprint": "fp-un-ticket", "enrichments": ["ticket_url"]},
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-un-ticket")
    # only the requested key is reverted -> NULL
    assert la.ticket_url is None
    assert la.ticket_type == "jira"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_unenrich_deleted_clears_to_false(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("fp-un-del", AlertStatus.FIRING, datetime.utcnow(), {"name": "a"})
    _set_last_alert_cols(db_session, "fp-un-del", deleted=True)

    resp = client.post(
        "/alerts/unenrich",
        headers={"x-api-key": "some-key"},
        json={"fingerprint": "fp-un-del", "enrichments": ["deleted"]},
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-un-del")
    # `deleted` is special-cased to False (not NULL)
    assert la.deleted is False


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_unenrich_unknown_key_returns_422(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("fp-un-bad", AlertStatus.FIRING, datetime.utcnow(), {"name": "a"})
    _set_last_alert_cols(db_session, "fp-un-bad", status="acknowledged")

    resp = client.post(
        "/alerts/unenrich",
        headers={"x-api-key": "some-key"},
        json={"fingerprint": "fp-un-bad", "enrichments": ["region"]},
    )
    # `region` has no typed column -> normalize_enrichments raises -> HTTP 422
    assert resp.status_code == 422

    la = _read_la(db_session, "fp-un-bad")
    # nothing was clobbered
    assert la.status == "acknowledged"


# --------------------------------------------------------------------------- #
# DELETE /alerts — soft-delete builds {deleted, assignee}
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_delete_route_sets_deleted_and_assignee(
    db_session, client, test_app, create_alert, elastic_client
):
    ts = datetime.utcnow()
    create_alert("fp-delete", AlertStatus.FIRING, ts, {"name": "a"})

    resp = client.request(
        "DELETE",
        "/alerts",
        headers={"x-api-key": "some-key"},
        json={"fingerprint": "fp-delete", "lastReceived": ts.isoformat()},
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-delete")
    assert la.deleted is True
    # delete also auto-assigns the acting user (NO_AUTH default email)
    assert la.assignee is not None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_delete_route_restore_clears_deleted(
    db_session, client, test_app, create_alert, elastic_client
):
    ts = datetime.utcnow()
    create_alert("fp-restore", AlertStatus.FIRING, ts, {"name": "a"})
    _set_last_alert_cols(db_session, "fp-restore", deleted=True)

    resp = client.request(
        "DELETE",
        "/alerts",
        headers={"x-api-key": "some-key"},
        json={
            "fingerprint": "fp-restore",
            "lastReceived": ts.isoformat(),
            "restore": True,
        },
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-restore")
    assert la.deleted is False


# --------------------------------------------------------------------------- #
# POST /alerts/{fp}/assign/{lr}
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_assign_route_sets_assignee_and_acknowledged(
    db_session, client, test_app, create_alert, elastic_client
):
    ts = datetime.utcnow()
    create_alert("fp-assign", AlertStatus.FIRING, ts, {"name": "a"})

    resp = client.post(
        f"/alerts/fp-assign/assign/{ts.isoformat()}",
        headers={"x-api-key": "some-key"},
        json={"dispose_on_new_alert": False},
    )
    assert resp.status_code == 200

    la = _read_la(db_session, "fp-assign")
    assert la.assignee is not None
    assert la.status == AlertStatus.ACKNOWLEDGED.value


# --------------------------------------------------------------------------- #
# last_received round-trip: tracking column -> canonical DTO wire string
# --------------------------------------------------------------------------- #
def test_last_received_tracking_roundtrip_to_dto(db_session, create_alert):
    fp = "fp-lr-roundtrip"
    create_alert(fp, AlertStatus.FIRING, datetime(2026, 1, 1, 0, 0, 0))

    # write last_received via the tracking path on a strictly-newer occurrence
    lr = datetime(2026, 5, 27, 8, 30, 15, 123000, tzinfo=timezone.utc)

    from src.models.db.alert import Alert

    new_alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="test",
        provider_id="test",
        fingerprint=fp,
        source="test",
        status="firing",
        severity="critical",
        name="lr",
        timestamp=datetime(2026, 5, 27, 8, 30, 15, tzinfo=timezone.utc),
    )
    db_session.add(new_alert)
    db_session.commit()

    set_last_alert(
        SINGLE_TENANT_UUID,
        new_alert,
        session=db_session,
        tracking={"last_received": lr, "firing_counter": 7},
    )

    db_session.expire_all()
    db_session.refresh(new_alert)
    dtos = convert_db_alerts_to_dto_alerts([new_alert], session=db_session)
    assert len(dtos) == 1
    dto = dtos[0]

    expected = lr.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    assert dto.last_received == expected
    assert dto.last_received == "2026-05-27T08:30:15.123Z"
    assert dto.firing_counter == 7
