import logging
from src.services.producers.base_event_handler import EventProducer
from src.services.producers.kafka_producer import KafkaEventProducer
from src.config.core import config

logger = logging.getLogger(__name__)

# Global producer instance for reuse
_kafka_producer_instance = None


async def get_event_producer() -> EventProducer:
    messaging_type = config("MESSAGING_TYPE", default="KAFKA").upper()
    global _kafka_producer_instance

    if messaging_type == "KAFKA":
        if _kafka_producer_instance is None:
            _kafka_producer_instance = KafkaEventProducer()
        return _kafka_producer_instance
    else:
        logger.warning(f"Unknown MESSAGING_TYPE: {messaging_type}, defaulting to KAFKA")
        if _kafka_producer_instance is None:
            _kafka_producer_instance = KafkaEventProducer()
        return _kafka_producer_instance
