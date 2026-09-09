"""
Tests for the internal EventSubscriber shutdown path.

`EventSubscriber.stop()` is synchronous (it joins consumer threads), so it must
not be awaited — doing so raises TypeError and the consumers are never stopped.
"""

import asyncio
import threading

import pytest

from src.services.producers.event_subscriber import EventSubscriber


class _FakeConsumerProvider:
    """Minimal stand-in for a consumer provider with a blocking consume loop."""

    def __init__(self, provider_id="p1", hang=False):
        self.provider_id = provider_id
        self.stopped = threading.Event()
        self._hang = hang

    def start_consume(self):
        # Runs on the subscriber's thread until stop_consume() is called.
        self.stopped.wait(timeout=30)

    def stop_consume(self):
        if not self._hang:
            self.stopped.set()

    def status(self):
        return "running"

    def __str__(self):
        return f"FakeConsumer({self.provider_id})"


@pytest.fixture
def subscriber():
    sub = EventSubscriber()
    yield sub
    # Make sure no test leaves a live thread behind.
    for consumer in list(sub.consumers):
        consumer.stopped.set()
    for thread in list(sub.consumer_threads):
        thread.join(timeout=5)


def test_stop_is_synchronous_not_awaitable(subscriber):
    """Pin the shape that caused the bug: stop() returns None, so `await`ing it
    raises TypeError. main.shutdown() must call it off the event loop instead."""
    assert asyncio.iscoroutinefunction(subscriber.stop) is False
    assert subscriber.stop() is None


def test_stop_stops_consumers_and_joins_threads(subscriber):
    provider = _FakeConsumerProvider()
    subscriber.add_consumer(provider)

    subscriber.stop(join_timeout=5)

    assert provider.stopped.is_set()
    assert subscriber.started is False
    assert subscriber.consumer_threads == []


def test_stop_is_bounded_when_a_consumer_hangs(subscriber):
    """A wedged consumer thread must not hold shutdown past the pod's
    terminationGracePeriodSeconds."""
    hanging = _FakeConsumerProvider(provider_id="hangs", hang=True)
    subscriber.add_consumer(hanging)

    subscriber.stop(join_timeout=0.1)  # returns despite the thread still running

    assert subscriber.started is False
    # Release the thread so the fixture can join it.
    hanging.stopped.set()


def test_one_failing_stop_does_not_skip_the_others(subscriber):
    class _Exploding(_FakeConsumerProvider):
        def stop_consume(self):
            raise RuntimeError("provider blew up on stop")

    exploding = _Exploding(provider_id="boom")
    healthy = _FakeConsumerProvider(provider_id="ok")
    subscriber.add_consumer(exploding)
    subscriber.add_consumer(healthy)

    subscriber.stop(join_timeout=0.1)

    assert healthy.stopped.is_set()
    exploding.stopped.set()


@pytest.mark.asyncio
async def test_run_in_executor_pattern_actually_stops_consumers(subscriber):
    """The replacement for `await event_subscriber.stop()`: offload the
    synchronous stop to a worker thread and bound it."""
    import functools

    provider = _FakeConsumerProvider()
    subscriber.add_consumer(provider)

    await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(
            None, functools.partial(subscriber.stop, join_timeout=5)
        ),
        timeout=10,
    )

    assert provider.stopped.is_set()
    assert subscriber.started is False
