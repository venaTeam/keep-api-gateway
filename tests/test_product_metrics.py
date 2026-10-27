"""
Phase 2 — user actions + alert status changes.

Covers the bounded allow-list (cardinality firewall), the ActionType -> product
action mapping (incl. dismiss detection), and the enrichment chokepoint recorder
that drives keep_user_action_total and keep_alert_status_change_total.
"""

from src.models.action_type import ActionType
from src.repositories.metrics import (
    alert_status_change_total,
    ui_page_loads_total,
    user_action_total,
)
from src.services.product_metrics import (
    alert_action_for,
    record_alert_enrichment,
    record_alert_status_change,
    record_page_load,
    record_user_action,
)

TENANT = "keep"


def _action(**labels) -> float:
    return user_action_total.labels(**labels)._value.get()


def _status(**labels) -> float:
    return alert_status_change_total.labels(**labels)._value.get()


def test_record_user_action_valid_increments():
    before = _action(
        tenant_id=TENANT,
        feature="alerts",
        action="assign",
        source="ui",
        result="success",
    )
    ok = record_user_action(
        tenant_id=TENANT, feature="alerts", action="assign", source="ui"
    )
    assert ok is True
    after = _action(
        tenant_id=TENANT,
        feature="alerts",
        action="assign",
        source="ui",
        result="success",
    )
    assert after == before + 1


def test_record_user_action_rejects_unknown_labels():
    # Negative test: unknown feature/action must be rejected and create no series.
    assert (
        record_user_action(tenant_id=TENANT, feature="bogus", action="assign") is False
    )
    assert (
        record_user_action(tenant_id=TENANT, feature="alerts", action="frobnicate")
        is False
    )


def test_record_page_load_bounded():
    before = ui_page_loads_total.labels(
        tenant_id=TENANT, route="alerts_feed"
    )._value.get()
    assert record_page_load(tenant_id=TENANT, route="alerts_feed") is True
    after = ui_page_loads_total.labels(
        tenant_id=TENANT, route="alerts_feed"
    )._value.get()
    assert after == before + 1
    # Unknown / unbounded route rejected (e.g. a free-text preset name).
    assert record_page_load(tenant_id=TENANT, route="preset:secret") is False


def test_record_alert_status_change_bounded():
    before = _status(tenant_id=TENANT, to_status="resolved")
    assert record_alert_status_change(tenant_id=TENANT, to_status="resolved") is True
    assert _status(tenant_id=TENANT, to_status="resolved") == before + 1
    # Unbounded/unknown status rejected.
    assert record_alert_status_change(tenant_id=TENANT, to_status="weird") is False


def test_alert_action_for_mapping():
    assert alert_action_for(ActionType.DELETE_ALERT, {}) == "delete"
    assert alert_action_for(ActionType.ACKNOWLEDGE, {}) == "assign"
    assert alert_action_for(ActionType.COMMENT, {}) == "add_note"
    assert alert_action_for(
        ActionType.MANUAL_STATUS_CHANGE, {"status": "resolved"}
    ) == ("change_status")
    # Dismiss is detected from the body, not the (shared) status action type.
    assert (
        alert_action_for(
            ActionType.MANUAL_STATUS_CHANGE,
            {"status": "suppressed", "dismiss_mode": "permanent"},
        )
        == "dismiss"
    )
    # System/rule enrichments are not user actions.
    assert alert_action_for(ActionType.WORKFLOW_ENRICH, {}) is None
    assert alert_action_for(ActionType.MAPPING_RULE_ENRICH, {}) is None


def test_record_alert_enrichment_status_change():
    a_before = _action(
        tenant_id=TENANT,
        feature="alerts",
        action="change_status",
        source="api",
        result="success",
    )
    s_before = _status(tenant_id=TENANT, to_status="resolved")

    record_alert_enrichment(
        tenant_id=TENANT,
        action_type=ActionType.MANUAL_STATUS_CHANGE,
        enrichments={"status": "resolved"},
        result="success",
    )

    assert (
        _action(
            tenant_id=TENANT,
            feature="alerts",
            action="change_status",
            source="api",
            result="success",
        )
        == a_before + 1
    )
    assert _status(tenant_id=TENANT, to_status="resolved") == s_before + 1


def test_record_alert_enrichment_dismiss():
    a_before = _action(
        tenant_id=TENANT,
        feature="alerts",
        action="dismiss",
        source="api",
        result="success",
    )
    s_before = _status(tenant_id=TENANT, to_status="suppressed")

    record_alert_enrichment(
        tenant_id=TENANT,
        action_type=ActionType.MANUAL_STATUS_CHANGE,
        enrichments={"status": "suppressed", "dismiss_mode": "permanent"},
        result="success",
    )

    assert (
        _action(
            tenant_id=TENANT,
            feature="alerts",
            action="dismiss",
            source="api",
            result="success",
        )
        == a_before + 1
    )
    assert _status(tenant_id=TENANT, to_status="suppressed") == s_before + 1


def test_record_alert_enrichment_dismiss_without_explicit_status():
    # Dismiss enrichments may carry only dismiss_mode (no status field); a dismiss
    # is still a suppression and must record to_status=suppressed.
    a_before = _action(
        tenant_id=TENANT,
        feature="alerts",
        action="dismiss",
        source="api",
        result="success",
    )
    s_before = _status(tenant_id=TENANT, to_status="suppressed")

    record_alert_enrichment(
        tenant_id=TENANT,
        action_type=ActionType.MANUAL_STATUS_CHANGE,
        enrichments={"dismiss_mode": "permanent"},
        result="success",
    )

    assert (
        _action(
            tenant_id=TENANT,
            feature="alerts",
            action="dismiss",
            source="api",
            result="success",
        )
        == a_before + 1
    )
    assert _status(tenant_id=TENANT, to_status="suppressed") == s_before + 1


def test_record_alert_enrichment_error_skips_status_change():
    # On error we record the action (result=error) but NOT a status change.
    a_before = _action(
        tenant_id=TENANT,
        feature="alerts",
        action="change_status",
        source="api",
        result="error",
    )
    s_before = _status(tenant_id=TENANT, to_status="acknowledged")

    record_alert_enrichment(
        tenant_id=TENANT,
        action_type=ActionType.MANUAL_STATUS_CHANGE,
        enrichments={"status": "acknowledged"},
        result="error",
    )

    assert (
        _action(
            tenant_id=TENANT,
            feature="alerts",
            action="change_status",
            source="api",
            result="error",
        )
        == a_before + 1
    )
    assert _status(tenant_id=TENANT, to_status="acknowledged") == s_before


def test_record_alert_enrichment_system_action_not_counted():
    # A workflow/rule enrichment must not produce a user-action series.
    record_alert_enrichment(
        tenant_id=TENANT,
        action_type=ActionType.WORKFLOW_ENRICH,
        enrichments={"foo": "bar"},
        result="success",
    )
    # No assertion on a specific series — covered by alert_action_for returning
    # None; this just exercises the no-op path without error.
