"""Dismiss writes only the dismiss columns — it no longer sets the alert status.

It used to set status='suppressed' alongside dismiss_mode. Since `status` is the
user override every reader coalesces over the provider value, that made
suppression a stored fact, and nothing sweeps the table — so an alert dismissed
until a deadline stayed suppressed forever once the deadline passed.

Suppression is now derived from dismiss_mode/dismissed_until at read time, which
leaves `status` free to hold whatever the alert reverts to when the dismissal
lapses. An explicit `status` travelling in the same payload is still applied; it
is a status change that happens to accompany a dismissal, not part of it.
"""

from datetime import datetime, timedelta, timezone

from src.models.db.alert import LastAlert
from src.repositories.db import normalize_enrichments


def _future(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _past(hours=2):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _last_alert(**kwargs) -> LastAlert:
    defaults = dict(
        tenant_id="test",
        fingerprint="fp",
        alert_id="00000000-0000-0000-0000-000000000000",
        timestamp=datetime.now(timezone.utc),
        first_timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return LastAlert(**defaults)


# === normalize_enrichments: dismiss no longer touches status ===


def test_direct_dismiss_mode_does_not_set_status():
    result = normalize_enrichments({"dismiss_mode": "permanent", "note": "x"})
    assert result["dismiss_mode"] == "permanent"
    assert "status" not in result


def test_direct_dismiss_until_mode_does_not_set_status():
    result = normalize_enrichments(
        {"dismiss_mode": "dismiss_until", "dismissed_until": "2099-01-01T00:00:00.000Z"}
    )
    assert result["dismiss_mode"] == "dismiss_until"
    assert result["dismissed_until"] == "2099-01-01T00:00:00.000Z"
    assert "status" not in result


def test_clearing_dismiss_mode_clears_the_deadline_with_it():
    result = normalize_enrichments({"dismiss_mode": None})
    assert result["dismiss_mode"] is None
    # A stale deadline must not outlive the dismissal it belonged to.
    assert result["dismissed_until"] is None


def test_clearing_dismiss_mode_keeps_explicit_status():
    # Change-status modal moving suppressed -> acknowledged while clearing dismiss.
    result = normalize_enrichments(
        {"dismiss_mode": None, "dismissed_until": None, "status": "acknowledged"}
    )
    assert result["status"] == "acknowledged"
    assert result["dismiss_mode"] is None


def test_explicit_status_travels_alongside_a_dismissal():
    result = normalize_enrichments({"dismiss_mode": "permanent", "status": "firing"})
    assert result["status"] == "firing"
    assert result["dismiss_mode"] == "permanent"


# === the derivation those writes rely on ===


def test_no_dismissal_means_override_then_provider():
    assert _last_alert().get_effective_status("firing") == "firing"
    assert _last_alert(status="acknowledged").get_effective_status("firing") == (
        "acknowledged"
    )


def test_permanent_dismissal_reads_as_suppressed():
    last_alert = _last_alert(dismiss_mode="permanent")
    assert last_alert.is_dismiss_active() is True
    assert last_alert.get_effective_status("firing") == "suppressed"


def test_dismiss_until_in_the_future_reads_as_suppressed():
    last_alert = _last_alert(
        dismiss_mode="dismiss_until", dismissed_until=_future()
    )
    assert last_alert.get_effective_status("firing") == "suppressed"


def test_dismiss_until_in_the_past_reverts_with_no_write():
    """The bug this replaced: an expired dismissal used to stay suppressed."""
    last_alert = _last_alert(
        dismiss_mode="dismiss_until", dismissed_until=_past()
    )
    assert last_alert.is_dismiss_active() is False
    assert last_alert.get_effective_status("firing") == "firing"
    # The row is untouched — nothing had to rewrite it for the alert to come back.
    assert last_alert.dismiss_mode == "dismiss_until"


def test_expired_dismissal_falls_back_to_the_override_not_the_provider():
    last_alert = _last_alert(
        status="acknowledged",
        dismiss_mode="dismiss_until",
        dismissed_until=_past(),
    )
    assert last_alert.get_effective_status("firing") == "acknowledged"


def test_live_dismissal_outranks_the_override():
    last_alert = _last_alert(
        status="acknowledged",
        dismiss_mode="permanent",
    )
    assert last_alert.get_effective_status("firing") == "suppressed"


def test_dismiss_until_without_a_deadline_is_not_active():
    assert _last_alert(dismiss_mode="dismiss_until").is_dismiss_active() is False


def test_naive_deadline_is_read_as_utc():
    # SQLite round-trips the timezone-aware column as naive.
    last_alert = _last_alert(
        dismiss_mode="dismiss_until",
        dismissed_until=_future().replace(tzinfo=None),
    )
    assert last_alert.is_dismiss_active() is True
