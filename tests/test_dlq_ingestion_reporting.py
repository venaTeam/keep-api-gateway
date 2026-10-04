"""
Tests that the producer marks an event diverted to the dead-letter topic.

Only the main topic is consumed, so an event on the DLQ topic is never processed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.producers.base_event_handler import DLQ_TASK_NAME, ProduceResult
from src.services.producers.kafka_producer import KafkaEventProducer


@pytest.mark.asyncio
async def test_produce_raises_when_both_topics_are_unreachable():
    """KAFKA_DLQ_BOOTSTRAP_SERVERS defaults to the *main* brokers, so an outage
    usually takes the fallback with it."""
    with patch("src.services.producers.kafka_producer.AIOKafkaProducer"):
        producer = KafkaEventProducer()

    producer._started = True
    producer.producer = MagicMock()
    producer.producer.send_and_wait = AsyncMock(side_effect=RuntimeError("no broker"))
    producer.dlq_producer = MagicMock()
    producer.dlq_producer.start = AsyncMock()
    producer.dlq_producer.send_and_wait = AsyncMock(
        side_effect=RuntimeError("no broker")
    )

    with pytest.raises(Exception):
        await producer.produce(event={"a": 1}, trace_id="t-2")


@pytest.mark.asyncio
async def test_kafka_producer_marks_the_dlq_sink():
    """The producer's return value carries the DLQ marker."""
    from src.services.producers.kafka_producer import KafkaEventProducer

    with patch("src.services.producers.kafka_producer.AIOKafkaProducer"):
        producer = KafkaEventProducer()

    producer._started = True
    producer.producer = MagicMock()
    producer.producer.send_and_wait = AsyncMock(side_effect=RuntimeError("no broker"))
    producer.dlq_producer = MagicMock()
    producer.dlq_producer.start = AsyncMock()
    producer.dlq_producer.send_and_wait = AsyncMock()

    task_name = await producer.produce(event={"a": 1}, trace_id="t-1")

    assert task_name == DLQ_TASK_NAME
    assert producer.last_produce_result() is ProduceResult.DLQ


@pytest.mark.asyncio
async def test_kafka_producer_health_reflects_connection_state():
    from src.services.producers.kafka_producer import KafkaEventProducer

    with patch("src.services.producers.kafka_producer.AIOKafkaProducer"):
        producer = KafkaEventProducer()

    producer.producer = MagicMock()
    producer.producer.start = AsyncMock(side_effect=OSError("no route to broker"))
    producer.dlq_producer = MagicMock()
    producer.dlq_producer.start = AsyncMock()

    healthy, detail = await producer.health(attempt_reconnect=True)
    assert healthy is False
    assert "OSError" in detail["last_error"]

    # Now let the bootstrap succeed: the probe-driven reconnect brings it up.
    producer.producer.start = AsyncMock()
    healthy, detail = await producer.health(attempt_reconnect=True)
    assert healthy is True
    assert "last_error" not in detail


@pytest.mark.asyncio
async def test_stop_closes_both_producers():
    """Started eagerly and never closed, they are reclaimed by process exit and
    aiokafka logs "Unclosed AIOKafkaProducer" on every restart."""
    with patch("src.services.producers.kafka_producer.AIOKafkaProducer"):
        producer = KafkaEventProducer()

    producer._started = True
    producer.producer = MagicMock(stop=AsyncMock())
    producer.dlq_producer = MagicMock(stop=AsyncMock())

    await producer.stop()

    producer.producer.stop.assert_awaited_once()
    producer.dlq_producer.stop.assert_awaited_once()
    assert producer._started is False


@pytest.mark.asyncio
async def test_stop_survives_a_broker_that_has_gone_away():
    """Shutdown must not hang or raise because a close failed."""
    with patch("src.services.producers.kafka_producer.AIOKafkaProducer"):
        producer = KafkaEventProducer()

    producer.producer = MagicMock(stop=AsyncMock(side_effect=OSError("gone")))
    producer.dlq_producer = MagicMock(stop=AsyncMock())

    await producer.stop()

    # The second producer is still closed despite the first one failing.
    producer.dlq_producer.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_eager_start_never_raises():
    """A broker that isn't up yet must not stop the gateway from starting."""
    from src.services.producers import factory

    producer = MagicMock()
    producer.start = AsyncMock(side_effect=OSError("brokers down"))

    with patch.object(factory, "get_event_producer", AsyncMock(return_value=producer)):
        result = await factory.start_event_producer()

    assert result is producer
    producer.start.assert_awaited_once()
