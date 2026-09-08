"""Cross-process SSE fan-out.

The broadcaster is per process, so a notification that arrives at one gateway
replica never reaches a browser whose stream lives in another. `broadcast()`
delivers locally and publishes the notification on a shared channel; every
process subscribes and delivers what other processes published. Redis being
unavailable degrades to local delivery, never to an error.
"""

import asyncio
import json
import os

import pytest

from src.repositories import metrics
from src.services import sse as sse_module
from src.services.sse_fanout import RedisFanout


class FakePubSubServer:
    """In-memory stand-in for one Redis server shared by every client."""

    def __init__(self):
        self.subscribers = []
        self.published = []
        self.down = False

    def client(self):
        return FakeRedisClient(self)


class FakeRedisClient:
    def __init__(self, server):
        self.server = server

    async def publish(self, channel, message):
        if self.server.down:
            raise ConnectionError("redis down")
        self.server.published.append((channel, message))
        for subscriber in list(self.server.subscribers):
            await subscriber.put(
                {"type": "message", "channel": channel, "data": message}
            )

    def pubsub(self):
        return FakePubSub(self.server)

    async def aclose(self):
        pass


class FakePubSub:
    def __init__(self, server):
        self.server = server
        self.queue = asyncio.Queue()

    async def subscribe(self, channel):
        if self.server.down:
            raise ConnectionError("redis down")
        self.server.subscribers.append(self.queue)

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout or 0.05)
        except asyncio.TimeoutError:
            return None

    async def aclose(self):
        if self.queue in self.server.subscribers:
            self.server.subscribers.remove(self.queue)


def _counter(counter, **labels):
    return (counter.labels(**labels) if labels else counter)._value.get()


async def _first_event(stream):
    await stream.__anext__()
    return await asyncio.wait_for(stream.__anext__(), timeout=2)


def test_a_notification_reaches_a_subscriber_held_by_another_process():
    async def run():
        server = FakePubSubServer()
        broker_a, broker_b = sse_module.SSEBroadcaster(), sse_module.SSEBroadcaster()
        fanout_a = RedisFanout(broker_a, server.client, channel="keep:sse", origin="a")
        fanout_b = RedisFanout(broker_b, server.client, channel="keep:sse", origin="b")
        await fanout_a.start()
        await fanout_b.start()
        await asyncio.sleep(0.1)
        stream_b = broker_b.subscribe("t1")
        await stream_b.__anext__()
        await fanout_a.broadcast("t1", "poll-alerts", {"x": 1})
        event = await asyncio.wait_for(stream_b.__anext__(), timeout=2)
        await stream_b.aclose()
        await fanout_a.stop()
        await fanout_b.stop()
        return event

    event = asyncio.run(run())
    assert event.startswith("event: poll-alerts\n")
    assert json.loads(event.split("data: ")[1]) == {"x": 1}


def test_the_publishing_process_delivers_locally_exactly_once():
    async def run():
        server = FakePubSubServer()
        broker = sse_module.SSEBroadcaster()
        fanout = RedisFanout(broker, server.client, channel="keep:sse", origin="a")
        await fanout.start()
        await asyncio.sleep(0.1)
        stream = broker.subscribe("t1")
        await stream.__anext__()
        await fanout.broadcast("t1", "poll-alerts", {})
        first = await asyncio.wait_for(stream.__anext__(), timeout=2)
        try:
            second = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
        except asyncio.TimeoutError:
            second = None
        await stream.aclose()
        await fanout.stop()
        return first, second

    first, second = asyncio.run(run())
    assert first.startswith("event: poll-alerts\n")
    assert second is None


def test_redis_being_down_degrades_to_local_delivery():
    async def run():
        server = FakePubSubServer()
        server.down = True
        broker = sse_module.SSEBroadcaster()
        fanout = RedisFanout(broker, server.client, channel="keep:sse", origin="a")
        await fanout.start()
        stream = broker.subscribe("t1")
        await stream.__anext__()
        errors_before = _counter(metrics.sse_fanout_errors_total, operation="publish")
        await fanout.broadcast("t1", "poll-alerts", {})
        event = await asyncio.wait_for(stream.__anext__(), timeout=2)
        await stream.aclose()
        await fanout.stop()
        return (
            event,
            _counter(metrics.sse_fanout_errors_total, operation="publish")
            - errors_before,
        )

    event, errors = asyncio.run(run())
    assert event.startswith("event: poll-alerts\n")
    assert errors == 1


def test_the_subscriber_recovers_when_redis_comes_back():
    async def run():
        server = FakePubSubServer()
        server.down = True
        broker_a, broker_b = sse_module.SSEBroadcaster(), sse_module.SSEBroadcaster()
        fanout_a = RedisFanout(broker_a, server.client, channel="keep:sse", origin="a")
        fanout_b = RedisFanout(
            broker_b,
            server.client,
            channel="keep:sse",
            origin="b",
            reconnect_delay=0.05,
        )
        await fanout_a.start()
        await fanout_b.start()
        await asyncio.sleep(0.2)
        server.down = False
        await asyncio.sleep(0.3)
        stream_b = broker_b.subscribe("t1")
        await stream_b.__anext__()
        await fanout_a.broadcast("t1", "poll-alerts", {})
        event = await asyncio.wait_for(stream_b.__anext__(), timeout=2)
        await stream_b.aclose()
        await fanout_a.stop()
        await fanout_b.stop()
        return event

    assert asyncio.run(run()).startswith("event: poll-alerts\n")


def test_published_and_received_notifications_are_counted():
    async def run():
        server = FakePubSubServer()
        broker_a, broker_b = sse_module.SSEBroadcaster(), sse_module.SSEBroadcaster()
        fanout_a = RedisFanout(broker_a, server.client, channel="keep:sse", origin="a")
        fanout_b = RedisFanout(broker_b, server.client, channel="keep:sse", origin="b")
        await fanout_a.start()
        await fanout_b.start()
        await asyncio.sleep(0.1)
        published_before = _counter(metrics.sse_fanout_published_total)
        received_before = _counter(metrics.sse_fanout_received_total)
        await fanout_a.broadcast("t1", "poll-alerts", {})
        await asyncio.sleep(0.2)
        await fanout_a.stop()
        await fanout_b.stop()
        return (
            _counter(metrics.sse_fanout_published_total) - published_before,
            _counter(metrics.sse_fanout_received_total) - received_before,
        )

    assert asyncio.run(run()) == (1, 1)


@pytest.mark.skipif(
    not os.environ.get("SSE_FANOUT_TEST_REDIS_URL"), reason="needs a real Redis"
)
def test_fan_out_over_a_real_redis():
    import redis.asyncio as aioredis

    url = os.environ["SSE_FANOUT_TEST_REDIS_URL"]

    async def run():
        broker_a, broker_b = sse_module.SSEBroadcaster(), sse_module.SSEBroadcaster()

        def factory():
            return aioredis.from_url(url)

        fanout_a = RedisFanout(broker_a, factory, channel="keep:sse-test", origin="a")
        fanout_b = RedisFanout(broker_b, factory, channel="keep:sse-test", origin="b")
        await fanout_a.start()
        await fanout_b.start()
        await asyncio.sleep(0.3)
        stream_b = broker_b.subscribe("t1")
        await stream_b.__anext__()
        await fanout_a.broadcast("t1", "poll-alerts", {"real": True})
        event = await asyncio.wait_for(stream_b.__anext__(), timeout=3)
        await stream_b.aclose()
        await fanout_a.stop()
        await fanout_b.stop()
        return event

    assert asyncio.run(run()).startswith("event: poll-alerts\n")


# The process-wide entry points: `broadcast()`, the synchronous `notify_sse()`
# used by the alert and incident routes, and the `/sse/notify` route the event
# handler calls, all go through the fan-out once `start_fanout()` enabled it.


def _configure(monkeypatch, server, mode):
    monkeypatch.setattr(sse_module, "SSE_FANOUT", mode)
    monkeypatch.setattr(sse_module, "sse_broadcaster", sse_module.SSEBroadcaster())
    monkeypatch.setattr(
        sse_module, "redis_client_factory", lambda **settings: server.client
    )


async def _peer_process(server):
    broker = sse_module.SSEBroadcaster()
    fanout = RedisFanout(broker, server.client, channel="keep:sse", origin="peer")
    await fanout.start()
    await asyncio.sleep(0.1)
    stream = broker.subscribe("t1")
    await stream.__anext__()
    return fanout, stream


async def _next_or_none(stream, timeout):
    try:
        return await asyncio.wait_for(stream.__anext__(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def test_broadcast_stays_local_when_fan_out_is_disabled(monkeypatch):
    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "none")
        await sse_module.start_fanout()
        peer_fanout, peer_stream = await _peer_process(server)
        local_stream = sse_module.sse_broadcaster.subscribe("t1")
        await local_stream.__anext__()
        await sse_module.broadcast("t1", "poll-alerts", {})
        local = await _next_or_none(local_stream, 2)
        remote = await _next_or_none(peer_stream, 0.3)
        await local_stream.aclose()
        await peer_stream.aclose()
        await peer_fanout.stop()
        await sse_module.stop_fanout()
        return local, remote

    local, remote = asyncio.run(run())
    assert local.startswith("event: poll-alerts\n")
    assert remote is None


def test_broadcast_reaches_other_processes_when_fan_out_is_enabled(monkeypatch):
    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        await sse_module.start_fanout()
        await asyncio.sleep(0.1)
        peer_fanout, peer_stream = await _peer_process(server)
        await sse_module.broadcast("t1", "poll-alerts", {"fp": "x"})
        remote = await _next_or_none(peer_stream, 2)
        await peer_stream.aclose()
        await peer_fanout.stop()
        await sse_module.stop_fanout()
        return remote

    remote = asyncio.run(run())
    assert remote.startswith("event: poll-alerts\n")
    assert json.loads(remote.split("data: ")[1]) == {"fp": "x"}


def test_notify_sse_from_synchronous_code_reaches_other_processes(monkeypatch):
    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        await sse_module.start_fanout()
        await asyncio.sleep(0.1)
        peer_fanout, peer_stream = await _peer_process(server)
        sse_module.notify_sse("t1", "incident-change", {"incident_id": "i1"})
        remote = await _next_or_none(peer_stream, 2)
        await peer_stream.aclose()
        await peer_fanout.stop()
        await sse_module.stop_fanout()
        return remote

    remote = asyncio.run(run())
    assert remote.startswith("event: incident-change\n")


def test_the_notify_route_reaches_other_processes(monkeypatch):
    from src.routes import sse_routes

    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        await sse_module.start_fanout()
        await asyncio.sleep(0.1)
        peer_fanout, peer_stream = await _peer_process(server)
        await sse_routes.sse_notify(
            sse_routes.SSENotification(
                tenant_id="t1", event="poll-alerts", data={"alerts": []}
            )
        )
        remote = await _next_or_none(peer_stream, 2)
        await peer_stream.aclose()
        await peer_fanout.stop()
        await sse_module.stop_fanout()
        return remote

    remote = asyncio.run(run())
    assert remote.startswith("event: poll-alerts\n")
    assert json.loads(remote.split("data: ")[1]) == {"alerts": []}


def test_start_fanout_passes_the_redis_settings_to_the_client_factory(monkeypatch):
    seen = {}

    def factory(**settings):
        seen.update(settings)
        return FakePubSubServer().client

    monkeypatch.setattr(sse_module, "SSE_FANOUT", "redis")
    monkeypatch.setattr(sse_module, "SSE_FANOUT_CHANNEL", "keep:sse-x")
    monkeypatch.setattr(sse_module, "REDIS_HOST", "valkey")
    monkeypatch.setattr(sse_module, "REDIS_PORT", 6380)
    monkeypatch.setattr(sse_module, "REDIS_USERNAME", "u")
    monkeypatch.setattr(sse_module, "REDIS_PASSWORD", "p")
    monkeypatch.setattr(sse_module, "REDIS_SENTINEL_ENABLED", False)
    monkeypatch.setattr(sse_module, "REDIS_SENTINEL_HOSTS", "s1:26379")
    monkeypatch.setattr(sse_module, "REDIS_SENTINEL_SERVICE_NAME", "mymaster")
    monkeypatch.setattr(sse_module, "REDIS_DB", 3)
    monkeypatch.setattr(sse_module, "REDIS_SSL", True)
    monkeypatch.setattr(sse_module, "REDIS_KEY_PREFIX", "acme:")
    monkeypatch.setattr(sse_module, "redis_client_factory", factory)

    async def run():
        await sse_module.start_fanout()
        channel = sse_module._fanout._channel
        await sse_module.stop_fanout()
        return channel, sse_module._fanout

    channel, after_stop = asyncio.run(run())
    assert channel == "acme:keep:sse-x"
    assert after_stop is None
    assert seen == {
        "host": "valkey",
        "port": 6380,
        "db": 3,
        "ssl": True,
        "username": "u",
        "password": "p",
        "sentinel_hosts": None,
        "sentinel_service": "mymaster",
    }


# A shared Redis hands each application a database index and a key prefix;
# the clients must select the database and, since pub/sub channels are
# instance-wide whatever the database, the channel must carry the prefix.


def test_the_direct_client_selects_the_database_and_tls():
    from redis.asyncio.connection import SSLConnection

    from src.services.sse_fanout import redis_client_factory

    client = redis_client_factory(
        host="redis.example",
        port=6380,
        db=3,
        ssl=True,
        username="u",
        password="p",
        sentinel_hosts=None,
        sentinel_service="mymaster",
    )()
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("redis.example", 6380, 3)
    assert client.connection_pool.connection_class is SSLConnection


def test_the_sentinel_client_selects_the_database():
    from src.services.sse_fanout import redis_client_factory

    client = redis_client_factory(
        host="ignored",
        port=6379,
        db=3,
        ssl=False,
        username=None,
        password=None,
        sentinel_hosts="s1:26379, s2",
        sentinel_service="mymaster",
    )()
    pool = client.connection_pool
    assert pool.service_name == "mymaster"
    assert pool.connection_kwargs["db"] == 3
    assert [
        (
            s.connection_pool.connection_kwargs["host"],
            s.connection_pool.connection_kwargs["port"],
        )
        for s in pool.sentinel_manager.sentinels
    ] == [("s1", 26379), ("s2", 26379)]
