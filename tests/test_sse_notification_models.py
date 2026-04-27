"""Tests for SSE notification pydantic models.

Verifies that SSENotification correctly accepts typed data payloads
(AlertNotifyData, PresetNotifyData, IncidentNotifyData) and rejects
invalid types like plain strings.
"""

import pytest
from pydantic import ValidationError

from src.routes.sse_routes import (
    AlertNotifyData,
    IncidentNotifyData,
    PresetNotifyData,
    SSENotification,
)


class TestSSENotificationModels:
    """Test SSENotification accepts all valid data payload types."""

    def test_accepts_alert_notify_data(self):
        notif = SSENotification(
            tenant_id="t1",
            event="poll-alerts",
            data={"alerts": [{"id": "a1", "name": "test"}]},
        )
        assert isinstance(notif.data, AlertNotifyData)
        assert len(notif.data.alerts) == 1

    def test_accepts_preset_notify_data(self):
        notif = SSENotification(
            tenant_id="t1",
            event="poll-presets",
            data={"preset_names": ["feed", "critical"]},
        )
        assert isinstance(notif.data, PresetNotifyData)
        assert notif.data.preset_names == ["feed", "critical"]

    def test_accepts_incident_notify_data(self):
        notif = SSENotification(
            tenant_id="t1",
            event="incident-change",
            data={"incident_ids": ["inc-1", "inc-2"]},
        )
        assert isinstance(notif.data, IncidentNotifyData)
        assert notif.data.incident_ids == ["inc-1", "inc-2"]

    def test_accepts_empty_dict(self):
        """Empty dict should fall through to the dict fallback."""
        notif = SSENotification(
            tenant_id="t1",
            event="connected",
            data={},
        )
        # Empty dict doesn't match any typed model (all require fields), so falls to dict
        assert isinstance(notif.data, dict)

    def test_accepts_generic_dict(self):
        notif = SSENotification(
            tenant_id="t1",
            event="topology-update",
            data={"custom_key": "custom_value"},
        )
        assert isinstance(notif.data, dict)

    def test_rejects_string_data(self):
        """This was the original bug: json.dumps() produced a string."""
        with pytest.raises(ValidationError):
            SSENotification(
                tenant_id="t1",
                event="poll-presets",
                data='["feed", "critical"]',
            )

    def test_default_data_is_empty(self):
        notif = SSENotification(
            tenant_id="t1",
            event="connected",
        )
        assert notif.data == {}
