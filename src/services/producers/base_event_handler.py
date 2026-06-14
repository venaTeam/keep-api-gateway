import abc
from enum import Enum

class EventType(str, Enum):
    ALERT = "alert"
    INCIDENT = "incident"
    ENRICH = "enrich"
    BATCH_ENRICH = "batch_enrich"
    DELETE = "delete"

class EventProducer(abc.ABC):
    @abc.abstractmethod
    async def produce(self, event: dict, event_type: EventType = EventType.ALERT, **kwargs):
        pass
