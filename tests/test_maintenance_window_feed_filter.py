"""The feed hides alerts whose "Hide From Feed" window is still active.

The event-handler stores alerts matched by a hide-mode window and records the
window's id on lastalert. The gateway's feed query hides such a row only while
that window exists, is enabled, is in hide mode and has not ended.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.models.db.alert import Alert, LastAlert
from src.models.db.maintenance_window import MaintenanceWindowRule
from src.models.query import QueryDto
from src.repositories.alerts import query_last_alerts
from src.repositories.db import get_last_alerts
from src.services.maintenance_windows import release_alerts_no_longer_matched
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.client import client, test_app  # noqa: F401


def _now():
    return datetime.utcnow()


def _window(db_session, tenant_id=SINGLE_TENANT_UUID, **overrides):
    values = dict(
        tenant_id=tenant_id,
        name="DB upgrade",
        description="",
        start_time=_now() - timedelta(hours=1),
        end_time=_now() + timedelta(hours=1),
        created_by="test_user",
        cel_query='source == "test"',
        enabled=True,
        suppress=False,
        ignore_statuses=[],
    )
    values.update(overrides)
    window = MaintenanceWindowRule(**values)
    db_session.add(window)
    db_session.commit()
    db_session.refresh(window)
    return window


def _alert(db_session, fingerprint, maintenance_window_id=None):
    now = _now()
    alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="test",
        provider_id="test",
        fingerprint=fingerprint,
        source="test",
        severity="high",
        status="firing",
        name=fingerprint,
        timestamp=now,
    )
    db_session.add(alert)
    db_session.commit()
    db_session.add(
        LastAlert(
            tenant_id=SINGLE_TENANT_UUID,
            fingerprint=fingerprint,
            alert_id=alert.id,
            timestamp=now,
            first_timestamp=now,
            maintenance_window_id=maintenance_window_id,
        )
    )
    db_session.commit()


def _feed():
    alerts = query_last_alerts(
        SINGLE_TENANT_UUID, QueryDto(cel="", limit=100, offset=0)
    )
    return sorted(alert.fingerprint for alert in alerts)


def test_active_hide_window_hides_the_alert(db_session):
    window = _window(db_session)
    _alert(db_session, "hidden", window.id)
    _alert(db_session, "visible")

    assert _feed() == ["visible"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_time": _now() - timedelta(minutes=1)},
        {"enabled": False},
        {"suppress": True},
    ],
    ids=["ended", "disabled", "switched-to-suppress"],
)
def test_alert_shows_once_the_window_is_no_longer_active(db_session, overrides):
    window = _window(db_session, **overrides)
    _alert(db_session, "revealed", window.id)

    assert _feed() == ["revealed"]


def test_alert_shows_when_its_window_is_deleted(db_session):
    window = _window(db_session)
    _alert(db_session, "revealed", window.id)
    db_session.delete(window)
    db_session.commit()

    assert _feed() == ["revealed"]


def test_alert_shows_when_its_window_is_cut_short(db_session):
    window = _window(db_session)
    _alert(db_session, "revealed", window.id)
    assert _feed() == []

    window.end_time = _now() - timedelta(seconds=1)
    db_session.add(window)
    db_session.commit()

    assert _feed() == ["revealed"]


def test_alert_shows_when_its_window_is_rescheduled_into_the_future(db_session):
    window = _window(db_session)
    _alert(db_session, "revealed", window.id)
    assert _feed() == []

    window.start_time = _now() + timedelta(days=1)
    window.end_time = _now() + timedelta(days=1, hours=1)
    db_session.add(window)
    db_session.commit()

    assert _feed() == ["revealed"]


def test_listing_last_alerts_leaves_out_hidden_ones(db_session):
    window = _window(db_session)
    _alert(db_session, "hidden", window.id)
    _alert(db_session, "visible")

    listed = sorted(a.fingerprint for a in get_last_alerts(SINGLE_TENANT_UUID))
    assert listed == ["visible"]

    by_fingerprint = get_last_alerts(SINGLE_TENANT_UUID, fingerprints=["hidden"])
    assert [a.fingerprint for a in by_fingerprint] == ["hidden"]


def test_window_of_another_tenant_does_not_hide(db_session):
    from src.models.db.tenant import Tenant

    db_session.add(Tenant(id="other-tenant", name="other-tenant"))
    db_session.commit()
    window = _window(db_session, tenant_id="other-tenant")
    _alert(db_session, "visible", window.id)

    assert _feed() == ["visible"]


# --------------------------------------------------------------------------- #
# Open feeds are told to reload when a window is edited or deleted
# --------------------------------------------------------------------------- #
def _window_payload(**overrides):
    payload = {
        "name": "DB upgrade",
        "description": "",
        "cel_query": 'source == "test"',
        "start_time": datetime.utcnow().isoformat() + "Z",
        "duration_seconds": 3600,
        "suppress": False,
        "enabled": True,
        "ignore_statuses": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_editing_or_deleting_a_window_notifies_open_feeds(db_session, client, test_app):
    headers = {"x-api-key": "some-key"}
    with patch("src.routes.maintenance.notify_sse") as notify:
        created = client.post("/maintenance", headers=headers, json=_window_payload())
        assert created.status_code == 200
        notify.assert_not_called()

        rule_id = created.json()["id"]
        updated = client.put(
            f"/maintenance/{rule_id}",
            headers=headers,
            json=_window_payload(enabled=False),
        )
        assert updated.status_code == 200
        assert notify.call_args_list[-1].args == (SINGLE_TENANT_UUID, "poll-alerts", {})

        deleted = client.delete(f"/maintenance/{rule_id}", headers=headers)
        assert deleted.status_code == 200
        assert notify.call_count == 2


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("tagged", ["hidden", "frozen"])
def test_resaving_a_window_that_still_matches_returns_the_rule(
    db_session, client, test_app, tagged
):
    """The re-check commits again even when it releases nothing, which expires
    the rule; the response must still be built from a loaded rule."""
    headers = {"x-api-key": "some-key"}
    with patch("src.routes.maintenance.notify_sse"):
        created = client.post("/maintenance", headers=headers, json=_window_payload())
        rule_id = created.json()["id"]
        if tagged == "hidden":
            _alert(db_session, "still-hidden", rule_id)
        else:
            _frozen_alert(db_session, "still-frozen", rule_id, held_name="renamed")

        updated = client.put(
            f"/maintenance/{rule_id}", headers=headers, json=_window_payload()
        )

    assert updated.status_code == 200
    assert updated.json()["id"] == rule_id
    assert updated.json()["cel_query"] == 'source == "test"'


# --------------------------------------------------------------------------- #
# Editing a window's filter or ignore statuses re-checks the alerts it hid
# --------------------------------------------------------------------------- #
def _edit(db_session, window, **changes):
    for key, value in changes.items():
        setattr(window, key, value)
    db_session.add(window)
    db_session.commit()
    return release_alerts_no_longer_matched(db_session, window)


def test_alert_no_longer_matched_by_the_edited_filter_is_released(db_session):
    window = _window(db_session)
    _alert(db_session, "released", window.id)

    assert _edit(db_session, window, cel_query='source == "something-else"') == 1
    assert _feed() == ["released"]


def test_alert_still_matched_by_the_edited_filter_stays_hidden(db_session):
    window = _window(db_session)
    _alert(db_session, "hidden", window.id)

    assert _edit(db_session, window, cel_query='source == "test" && severity == "high"') == 0
    assert _feed() == []


def test_newly_ignored_status_releases_the_alert(db_session):
    window = _window(db_session)
    _alert(db_session, "released", window.id)

    assert _edit(db_session, window, ignore_statuses=["firing"]) == 1
    assert _feed() == ["released"]


def test_filter_on_a_field_that_is_not_stored_keeps_the_alert_hidden(db_session):
    window = _window(db_session)
    _alert(db_session, "hidden", window.id)

    # `component` is an AlertDto field with no column on alert.
    assert _edit(db_session, window, cel_query='component == "db"') == 0
    assert _feed() == []


def test_alerts_of_other_windows_are_not_touched(db_session):
    edited = _window(db_session)
    other = _window(db_session)
    _alert(db_session, "other", other.id)

    _edit(db_session, edited, cel_query='source == "something-else"')

    assert _feed() == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_put_with_a_new_filter_releases_alerts(db_session, client, test_app):
    headers = {"x-api-key": "some-key"}
    with patch("src.routes.maintenance.notify_sse"):
        created = client.post("/maintenance", headers=headers, json=_window_payload())
        rule_id = created.json()["id"]
        _alert(db_session, "released", rule_id)
        assert _feed() == []

        updated = client.put(
            f"/maintenance/{rule_id}",
            headers=headers,
            json=_window_payload(cel_query='source == "something-else"'),
        )

    assert updated.status_code == 200
    assert _feed() == ["released"]


# --------------------------------------------------------------------------- #
# Frozen alerts: on screen when the window matched them
# --------------------------------------------------------------------------- #


def _frozen_alert(db_session, fingerprint, window_id, held_name="renamed"):
    """A visible row still showing its pre-window occurrence (named after the
    fingerprint), holding back a newer one named `held_name`."""
    _alert(db_session, fingerprint)
    held = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="test",
        provider_id="test",
        fingerprint=fingerprint,
        source="test",
        severity="high",
        status="firing",
        name=held_name,
        timestamp=_now() + timedelta(seconds=1),
    )
    db_session.add(held)
    db_session.commit()
    last_alert = db_session.get(LastAlert, (SINGLE_TENANT_UUID, fingerprint))
    last_alert.maintenance_frozen_window_id = window_id
    last_alert.maintenance_pending_alert_id = held.id
    last_alert.maintenance_pending_firing_count = 1
    db_session.add(last_alert)
    db_session.commit()
    return held


def _frozen_window_id(db_session, fingerprint):
    db_session.expire_all()
    return db_session.get(
        LastAlert, (SINGLE_TENANT_UUID, fingerprint)
    ).maintenance_frozen_window_id


def test_frozen_alert_stays_in_the_feed(db_session):
    window = _window(db_session)
    _frozen_alert(db_session, "frozen", window.id)

    assert _feed() == ["frozen"]


def test_edit_releases_a_frozen_alert_judged_by_its_held_back_occurrence(db_session):
    window = _window(db_session)
    held = _frozen_alert(db_session, "frozen", window.id, held_name="renamed")

    # The occurrence on screen still matches; the held-back one does not, so
    # the edited window no longer freezes the alert.
    assert _edit(db_session, window, cel_query='name == "frozen"') == 1
    assert _frozen_window_id(db_session, "frozen") is None
    # The held-back occurrence stays for the watcher to catch up to.
    db_session.expire_all()
    last_alert = db_session.get(LastAlert, (SINGLE_TENANT_UUID, "frozen"))
    assert last_alert.maintenance_pending_alert_id == held.id
    assert last_alert.maintenance_pending_firing_count == 1


def test_edit_keeps_a_frozen_alert_it_still_matches(db_session):
    window = _window(db_session)
    _frozen_alert(db_session, "frozen", window.id, held_name="renamed")

    assert _edit(db_session, window, cel_query='name == "renamed"') == 0
    assert _frozen_window_id(db_session, "frozen") == window.id


# --------------------------------------------------------------------------- #
# Several windows per alert (lastalertmaintenancewindow tags)
# --------------------------------------------------------------------------- #


def _tag(db_session, fingerprint, window_id, kind="hide"):
    from src.models.db.alert import LastAlertMaintenanceWindow

    db_session.add(
        LastAlertMaintenanceWindow(
            tenant_id=SINGLE_TENANT_UUID,
            fingerprint=fingerprint,
            kind=kind,
            maintenance_window_id=window_id,
        )
    )
    db_session.commit()


def _tags(db_session, fingerprint):
    from sqlmodel import select as sm_select

    from src.models.db.alert import LastAlertMaintenanceWindow

    db_session.expire_all()
    rows = db_session.exec(
        sm_select(LastAlertMaintenanceWindow).where(
            LastAlertMaintenanceWindow.fingerprint == fingerprint
        )
    ).all()
    return sorted((row.kind, row.maintenance_window_id) for row in rows)


def _marker(db_session, fingerprint, column="maintenance_window_id"):
    db_session.expire_all()
    return getattr(db_session.get(LastAlert, (SINGLE_TENANT_UUID, fingerprint)), column)


def test_alert_stays_hidden_while_any_tagged_window_is_active(db_session):
    """SegevSi's review: the marker's window ended, another matching window
    is still running."""
    ended = _window(db_session, end_time=_now() - timedelta(minutes=1))
    running = _window(db_session, end_time=_now() + timedelta(hours=1))
    _alert(db_session, "two-windows", ended.id)
    _tag(db_session, "two-windows", ended.id)
    _tag(db_session, "two-windows", running.id)

    assert _feed() == []


def test_alert_shows_once_every_tagged_window_stopped(db_session):
    ended = _window(db_session, end_time=_now() - timedelta(minutes=1))
    disabled = _window(db_session, enabled=False)
    _alert(db_session, "all-stopped", ended.id)
    _tag(db_session, "all-stopped", ended.id)
    _tag(db_session, "all-stopped", disabled.id)

    assert _feed() == ["all-stopped"]


def test_freeze_tags_do_not_hide(db_session):
    window = _window(db_session)
    _alert(db_session, "frozen-tag")
    _tag(db_session, "frozen-tag", window.id, kind="freeze")

    assert _feed() == ["frozen-tag"]


def test_edit_moves_the_marker_to_the_other_window_that_still_hides(db_session):
    edited = _window(db_session, end_time=_now() + timedelta(hours=2))
    other = _window(db_session, end_time=_now() + timedelta(hours=1))
    _alert(db_session, "still-hidden", edited.id)
    _tag(db_session, "still-hidden", edited.id)
    _tag(db_session, "still-hidden", other.id)

    assert _edit(db_session, edited, cel_query='source == "something-else"') == 1
    assert _marker(db_session, "still-hidden") == other.id
    assert _tags(db_session, "still-hidden") == [("hide", other.id)]
    assert _feed() == []

    assert _edit(db_session, other, cel_query='source == "something-else"') == 1
    assert _marker(db_session, "still-hidden") is None
    assert _tags(db_session, "still-hidden") == []
    assert _feed() == ["still-hidden"]


def test_edit_keeps_a_frozen_alert_frozen_by_the_other_window(db_session):
    edited = _window(db_session, end_time=_now() + timedelta(hours=2))
    other = _window(db_session, end_time=_now() + timedelta(hours=1))
    _frozen_alert(db_session, "frozen-two", edited.id, held_name="renamed")
    _tag(db_session, "frozen-two", edited.id, kind="freeze")
    _tag(db_session, "frozen-two", other.id, kind="freeze")

    assert _edit(db_session, edited, cel_query='name == "frozen-two"') == 1
    assert _marker(db_session, "frozen-two", "maintenance_frozen_window_id") == other.id
    assert _tags(db_session, "frozen-two") == [("freeze", other.id)]


def test_edit_reaches_a_row_that_lists_the_window_only_as_a_tag(db_session):
    edited = _window(db_session, end_time=_now() + timedelta(hours=1))
    marker = _window(db_session, end_time=_now() + timedelta(hours=2))
    _alert(db_session, "tag-only", marker.id)
    _tag(db_session, "tag-only", marker.id)
    _tag(db_session, "tag-only", edited.id)

    assert _edit(db_session, edited, cel_query='source == "something-else"') == 1
    assert _marker(db_session, "tag-only") == marker.id
    assert _tags(db_session, "tag-only") == [("hide", marker.id)]
