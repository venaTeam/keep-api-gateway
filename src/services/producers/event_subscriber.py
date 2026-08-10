import logging
import threading
from src.providers.base.base_provider import BaseProvider
from src.providers.providers_factory import ProvidersFactory


class EventSubscriber:
    @staticmethod
    def get_instance() -> "EventSubscriber":
        if not hasattr(EventSubscriber, "_instance"):
            EventSubscriber._instance = EventSubscriber()
        return EventSubscriber._instance

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.consumers = []
        self.consumer_threads = []
        self.started = False

    def status(self):
        """Returns the status of the consumers"""
        return {
            "consumers": [
                {
                    "provider_id": cp.provider_id,
                    "status": cp.status(),
                }
                for cp in self.consumers
            ]
        }

    def add_consumer(self, consumer_provider: BaseProvider):
        """Add a consumer (on installation)

        Args:
            consumer_provider (_type_): _description_
        """
        self.logger.info("Adding consumer %s", consumer_provider)
        # start the consumer in a separate thread
        thread = threading.Thread(
            target=consumer_provider.start_consume,
            name=f"consumer-{consumer_provider}",
        )
        thread.start()
        self.consumers.append(consumer_provider)
        self.consumer_threads.append(thread)
        self.logger.info(
            "Started consumer thread for event provider %s", consumer_provider
        )

    async def start(self):
        """Runs the event subscriber in server mode"""
        if self.started:
            self.logger.info("Event subscriber already started")
            return
        self.logger.info("Starting event subscriber")
        consumer_providers = ProvidersFactory.get_consumer_providers()
        for consumer_provider in consumer_providers:
            # get the consumer for the event provider
            self.logger.info(
                "Getting consumer for event provider %s", consumer_provider
            )
            # start the consumer in a separate thread
            thread = threading.Thread(
                target=consumer_provider.start_consume,
                name=f"consumer-{consumer_provider}",
            )
            thread.start()
            self.consumers.append(consumer_provider)
            self.consumer_threads.append(thread)
            self.logger.info(
                "Started consumer thread for event provider %s", consumer_provider
            )
        self.started = True

    def remove_consumer(self, provider_id: str):
        """Remove a consumer (on uninstallation)

        Args:
            consumer_provider (_type_): _description_
        """
        self.logger.info("Removing consumer %s", provider_id)
        for cp in self.consumers:
            if cp.provider_id == provider_id:
                cp.stop_consume()
                break
        self.logger.info("Removed consumer %s", provider_id)

    def stop(self, join_timeout: float | None = None):
        """Stops the consumers.

        Synchronous on purpose (it joins threads) — callers on the event loop
        must not `await` it; see `src/main.py:shutdown`, which offloads it.

        Args:
            join_timeout: per-thread join bound. None waits forever, which can
                hang shutdown if a consumer thread is stuck in a provider call.
        """
        for consumer in self.consumers:
            self.logger.info("Stopping consumer %s", consumer)
            try:
                consumer.stop_consume()
            except Exception:
                self.logger.exception("Failed to stop consumer %s", consumer)
            else:
                self.logger.info("Stopped consumer %s", consumer)

        # Join the threads
        self.logger.info("Joining consumer threads")
        for thread in self.consumer_threads:
            thread.join(timeout=join_timeout)

            if thread.is_alive():
                self.logger.warning(
                    "Consumer thread %s did not stop within %ss; abandoning it",
                    thread.name,
                    join_timeout,
                )
        self.consumers = []
        self.consumer_threads = []
        self.started = False
        self.logger.info("Joined consumer threads")
