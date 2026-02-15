import abc

class EventProducer(abc.ABC):
    @abc.abstractmethod
    async def produce(self, event: dict, **kwargs):
        pass
