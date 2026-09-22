import datetime
from enum import Enum
import json
from unittest.mock import AsyncMock
import uuid

import pytest
from pydantic import BaseModel

from src.models.action_type import ActionType
from src.models.db.incident import IncidentDismissMode, IncidentStatus
from src.services.enrichments_bl import EnrichmentsBl, _event_safe_value
from src.services.producers.base_event_handler import EventProducer, EventType


def test_event_safe_value_primitives():
    assert _event_safe_value(None) is None
    assert _event_safe_value("test") == "test"
    assert _event_safe_value(123) == 123
    assert _event_safe_value(45.67) == 45.67
    assert _event_safe_value(True) is True
    assert _event_safe_value(False) is False


def test_event_safe_value_datetime():
    # Naive datetime: assumes UTC and produces ISO 8601 with milliseconds and Z
    dt_naive = datetime.datetime(2026, 9, 22, 14, 30, 0, 123456)
    assert _event_safe_value(dt_naive) == "2026-09-22T14:30:00.123Z"

    # Naive datetime with zero microseconds
    dt_zero_micros = datetime.datetime(2026, 9, 22, 14, 30, 0)
    assert _event_safe_value(dt_zero_micros) == "2026-09-22T14:30:00.000Z"

    # Timezone-aware datetime: converted to UTC
    tz_plus_3 = datetime.timezone(datetime.timedelta(hours=3))
    dt_aware = datetime.datetime(2026, 9, 22, 17, 30, 0, 500000, tzinfo=tz_plus_3)
    assert _event_safe_value(dt_aware) == "2026-09-22T14:30:00.500Z"

    # Date object
    d = datetime.date(2026, 9, 22)
    assert _event_safe_value(d) == "2026-09-22"

    # Time object
    t = datetime.time(14, 30, 0)
    assert _event_safe_value(t) == "14:30:00"


def test_event_safe_value_uuid():
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert _event_safe_value(u) == "12345678-1234-5678-1234-567812345678"


def test_event_safe_value_enums():
    assert _event_safe_value(IncidentStatus.SUPPRESSED) == "suppressed"
    assert _event_safe_value(IncidentDismissMode.PERMANENT) == "permanent"
    assert _event_safe_value(ActionType.INCIDENT_ENRICH) == ActionType.INCIDENT_ENRICH.value


def test_event_safe_value_nested_structures():
    u = uuid.uuid4()
    now = datetime.datetime(2026, 9, 22, 12, 0, 0, tzinfo=datetime.timezone.utc)
    data = {
        "str_key": "hello",
        "dt": now,
        "uuid": u,
        "status": IncidentStatus.ACKNOWLEDGED,
        "list": [
            now,
            u,
            {"nested_dt": now, "mode": IncidentDismissMode.DISMISS_UNTIL},
        ],
        "tuple": (IncidentStatus.FIRING, now),
        "set": {1, 2},
    }

    result = _event_safe_value(data)
    assert result["str_key"] == "hello"
    assert result["dt"] == "2026-09-22T12:00:00.000Z"
    assert result["uuid"] == str(u)
    assert result["status"] == "acknowledged"
    assert result["list"][0] == "2026-09-22T12:00:00.000Z"
    assert result["list"][1] == str(u)
    assert result["list"][2]["nested_dt"] == "2026-09-22T12:00:00.000Z"
    assert result["list"][2]["mode"] == "dismiss_until"
    assert result["tuple"] == ["firing", "2026-09-22T12:00:00.000Z"]

    # Verify standard json.dumps succeeds without custom encoder
    serialized = json.dumps(result)
    assert isinstance(serialized, str)


def test_event_safe_value_pydantic_model():
    class DummyModel(BaseModel):
        dt: datetime.datetime
        status: str

    model = DummyModel(
        dt=datetime.datetime(2026, 9, 22, 10, 0, 0, tzinfo=datetime.timezone.utc),
        status="firing",
    )
    result = _event_safe_value(model)
    assert result["status"] == "firing"
    assert result["dt"] == "2026-09-22T10:00:00.000Z"


@pytest.mark.asyncio
async def test_publish_enrichment_event_with_datetime():
    mock_producer = AsyncMock(spec=EventProducer)
    bl = EnrichmentsBl(tenant_id="tenant-123", db=None, event_producer=mock_producer)
    # Ensure db session cleanup is not needed
    bl.db_session = None

    dismissed_until = datetime.datetime(2026, 9, 22, 18, 0, 0, tzinfo=datetime.timezone.utc)
    enrichments = {
        "status": IncidentStatus.SUPPRESSED,
        "dismiss_mode": IncidentDismissMode.DISMISS_UNTIL,
        "dismissed_until": dismissed_until,
        "assignee": "admin@keep",
        "custom_id": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    }

    await bl.publish_enrichment_event(
        fingerprint="fp-123",
        enrichments=enrichments,
        action_type=ActionType.INCIDENT_ENRICH,
        action_callee="admin@keep",
        action_description="Suppressed incident",
        force=True,
    )

    mock_producer.produce.assert_called_once()
    kwargs = mock_producer.produce.call_args.kwargs

    event = kwargs["event"]
    assert event["status"] == "suppressed"
    assert event["dismiss_mode"] == "dismiss_until"
    assert event["dismissed_until"] == "2026-09-22T18:00:00.000Z"
    assert event["custom_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert event["action_type"] == ActionType.INCIDENT_ENRICH.value
    assert event["action_callee"] == "admin@keep"
    assert event["force"] is True

    # Crucial check: standard json.dumps produces valid json without exceptions
    raw_json = json.dumps(event)
    parsed = json.loads(raw_json)
    assert parsed["dismissed_until"] == "2026-09-22T18:00:00.000Z"
