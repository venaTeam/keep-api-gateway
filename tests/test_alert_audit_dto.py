"""Unit tests for AlertAuditDto.from_orm_list.

Regression coverage for the timeline endpoint blowing up (HTTP 500) when a
single audit row carries an action value this service's ActionType enum did
not know about — an enum that had drifted between repos. One unknown value
must not take down the whole timeline.
"""

from datetime import datetime
from types import SimpleNamespace

from src.models.action_type import ActionType
from src.models.alert_audit import AlertAuditDto


def _row(action, *, description="d", user_id="u", fingerprint="fp", row_id=None):
    return SimpleNamespace(
        id=row_id or f"id-{action}",
        timestamp=datetime(2026, 6, 14, 7, 56, 55),
        fingerprint=fingerprint,
        action=action,
        user_id=user_id,
        description=description,
        mentions=None,
    )


def test_workflow_enrich_is_a_valid_action():
    # The value the workflow/watcher writes; gateway enum must accept it.
    assert ActionType.WORKFLOW_ENRICH.value == "alert enriched by workflow"
    result = AlertAuditDto.from_orm_list([_row("alert enriched by workflow")])
    assert len(result) == 1
    assert result[0].action == ActionType.WORKFLOW_ENRICH


def test_invalid_action_row_is_skipped_not_fatal():
    rows = [
        _row("alert status manually changed", description="status"),
        _row("totally-unknown-action", description="bad"),
        _row("a comment was added to the alert", description="comment"),
    ]
    result = AlertAuditDto.from_orm_list(rows)
    actions = [dto.action for dto in result]
    # Bad row dropped; the valid rows around it survive.
    assert ActionType.MANUAL_STATUS_CHANGE in actions
    assert ActionType.COMMENT in actions
    assert len(result) == 2


def test_consecutive_identical_events_are_grouped_with_count():
    rows = [
        _row("alert enriched", description="same", user_id="u"),
        _row("alert enriched", description="same", user_id="u"),
        _row("alert enriched", description="same", user_id="u"),
    ]
    result = AlertAuditDto.from_orm_list(rows)
    assert len(result) == 1
    assert result[0].description.endswith("x3")
