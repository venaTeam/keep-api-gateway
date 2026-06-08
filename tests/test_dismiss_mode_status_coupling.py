"""normalize_enrichments couples a direct dismiss_mode write to the alert status.

The frontend now sends `dismiss_mode` directly (no legacy `dismissed` flag), so
the translation layer must suppress the alert when a dismiss_mode is set and
revert it when the dismiss_mode is cleared — matching the legacy `dismissed`
translation. An explicit caller-supplied status always wins.
"""

from src.repositories.db import normalize_enrichments


def test_direct_dismiss_mode_suppresses_status():
    result = normalize_enrichments({"dismiss_mode": "permanent", "note": "x"})
    assert result["status"] == "suppressed"
    assert result["dismiss_mode"] == "permanent"


def test_direct_dismiss_until_mode_suppresses_status():
    result = normalize_enrichments(
        {"dismiss_mode": "dismiss_until", "dismissed_until": "2099-01-01T00:00:00.000Z"}
    )
    assert result["status"] == "suppressed"
    assert result["dismiss_mode"] == "dismiss_until"
    assert result["dismissed_until"] == "2099-01-01T00:00:00.000Z"


def test_clearing_dismiss_mode_reverts_status_when_no_explicit_status():
    result = normalize_enrichments({"dismiss_mode": None, "dismissed_until": None})
    assert result["status"] is None
    assert result["dismiss_mode"] is None
    assert result["dismissed_until"] is None


def test_clearing_dismiss_mode_keeps_explicit_status():
    # Change-status modal moving suppressed -> acknowledged while clearing dismiss.
    result = normalize_enrichments(
        {"dismiss_mode": None, "dismissed_until": None, "status": "acknowledged"}
    )
    assert result["status"] == "acknowledged"
    assert result["dismiss_mode"] is None


def test_explicit_status_wins_over_implied_suppressed():
    result = normalize_enrichments({"dismiss_mode": "permanent", "status": "firing"})
    assert result["status"] == "firing"
    assert result["dismiss_mode"] == "permanent"
