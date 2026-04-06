from unittest.mock import AsyncMock, MagicMock, patch
import json
import pytest
from src.services.producers.base_event_handler import EventProducer
from src.services.producers.kafka_producer import KafkaEventProducer
from src.services.producers.factory import get_event_producer
from src.models.alert import AlertDto, AlertSeverity, AlertStatus
import os

@pytest.fixture
def mock_kafka_producer():
    with patch("src.services.producers.kafka_producer.AIOKafkaProducer") as mock:
        producer_instance = AsyncMock()
        mock.return_value = producer_instance
        yield producer_instance

@pytest.mark.asyncio
async def test_get_event_producer_kafka():
    # Mock environment to return KAFKA
    with patch.dict(os.environ, {"MESSAGING_TYPE": "KAFKA"}):
        # We need to reset the global instance for the test
        with patch("src.services.producers.factory._kafka_producer_instance", None):
            with patch("src.services.producers.factory.KafkaEventProducer") as MockProducer:
                producer = await get_event_producer()
                assert producer is not None
                MockProducer.assert_called_once()
                # Ensure it returns the mock instance
                assert producer == MockProducer.return_value

@pytest.mark.asyncio
async def test_kafka_producer_serialization(mock_kafka_producer):
    # Test that Pydantic models are serialized correctly
    producer = KafkaEventProducer()
    # Mock the internal producer
    producer.producer = mock_kafka_producer

    alert = AlertDto(
        id="test-id",
        name="test-alert",
        status=AlertStatus.FIRING,
        severity=AlertSeverity.INFO,
        lastReceived="2023-01-01T00:00:00.000Z",
        source=["test"]
    )

    # Payload with Pydantic model
    event_payload = {"alert": alert}

    await producer.produce(event=event_payload, trace_id="trace-123")

    # Verify send_and_wait was called
    mock_kafka_producer.send_and_wait.assert_called_once()

    # Verify the argument passed to send_and_wait
    args, _ = mock_kafka_producer.send_and_wait.call_args
    topic, val = args

    # Decode the JSON
    data = json.loads(val.decode("utf-8"))

    # Check that alert was serialized to a dict
    assert data["event"]["alert"]["id"] == "test-id"
    assert data["event"]["alert"]["status"] == "firing"
    assert data["trace_id"] == "trace-123"
