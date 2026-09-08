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
    """In-memory stand-in for one Redis server shared by every client. Like a
    real asyncio Redis client, it belongs to the event loop it was created on
    and refuses to be driven from another one."""

    def __init__(self):
        self.subscribers = []
        self.published = []
        self.down = False
        self.loop = asyncio.get_running_loop()

    def check_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("got Future attached to a different loop")

    def client(self):
        return FakeRedisClient(self)


class FakeRedisClient:
    def __init__(self, server):
        self.server = server

    async def publish(self, channel, message):
        self.server.check_loop()
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
        self.server.check_loop()
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


def test_notify_sse_from_a_worker_thread_reaches_other_processes(monkeypatch):
    """Synchronous routes run in worker threads with no event loop of their own;
    their notifications must still be published from the server's loop, where
    the fan-out's Redis client lives, instead of a throwaway loop."""

    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        await sse_module.start_fanout()
        await asyncio.sleep(0.1)
        peer_fanout, peer_stream = await _peer_process(server)
        await asyncio.get_running_loop().run_in_executor(
            None, sse_module.notify_sse, "t1", "incident-change", {"incident_id": "i1"}
        )
        remote = await _next_or_none(peer_stream, 2)
        await peer_stream.aclose()
        await peer_fanout.stop()
        await sse_module.stop_fanout()
        return remote

    remote = asyncio.run(run())
    assert remote is not None and remote.startswith("event: incident-change\n")


def test_notify_sse_delivers_locally_when_the_server_loop_has_stopped(monkeypatch):
    """A server loop that is open but no longer running (the lifespan has
    ended) accepts a scheduled notification and never executes it. The
    notification must reach the local broadcaster through a private loop
    instead of vanishing."""
    _configure(monkeypatch, None, "none")
    monkeypatch.setattr(sse_module, "_server_loop", None)
    stopped = asyncio.new_event_loop()
    try:
        stopped.run_until_complete(sse_module.start_fanout())
        before = _counter(
            metrics.sse_notifications_total,
            event="incident-change",
            outcome="no_subscriber",
        )
        sse_module.notify_sse("t1", "incident-change", {"incident_id": "i1"})
        after = _counter(
            metrics.sse_notifications_total,
            event="incident-change",
            outcome="no_subscriber",
        )
    finally:
        stopped.close()
    assert after == before + 1


def test_falling_back_to_a_private_loop_is_logged(monkeypatch, caplog):
    """The original bug hid because the private loop still served local
    subscribers; taking that path must leave a trace."""
    _configure(monkeypatch, None, "none")
    monkeypatch.setattr(sse_module, "_server_loop", None)
    with caplog.at_level("WARNING", logger="src.services.sse"):
        sse_module.notify_sse("t1", "incident-change", {"incident_id": "i1"})
    assert any("locally only" in record.getMessage() for record in caplog.records), [
        record.getMessage() for record in caplog.records
    ]


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


def test_an_empty_key_prefix_is_flagged_when_the_fan_out_starts(monkeypatch, caplog):
    """On a Redis shared between deployments the prefix is the only thing
    separating their channels; starting without one must be visible."""

    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        monkeypatch.setattr(sse_module, "REDIS_KEY_PREFIX", "")
        with caplog.at_level("WARNING", logger="src.services.sse"):
            await sse_module.start_fanout()
        await sse_module.stop_fanout()

    asyncio.run(run())
    assert any("REDIS_KEY_PREFIX" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def _notify_app():
    from fastapi import FastAPI

    from src.routes import sse_routes

    app = FastAPI()
    app.include_router(sse_routes.router, prefix="/sse")
    return app


def test_the_notify_route_rejects_requests_without_the_configured_token(monkeypatch):
    from fastapi.testclient import TestClient

    from src.routes import sse_routes

    _configure(monkeypatch, None, "none")
    monkeypatch.setattr(sse_routes, "SSE_NOTIFY_TOKEN", "s3cret")
    body = {"tenant_id": "t1", "event": "poll-alerts", "data": {}}
    with TestClient(_notify_app()) as client:
        assert client.post("/sse/notify", json=body).status_code == 401
        wrong = client.post(
            "/sse/notify", json=body, headers={"X-Keep-Notify-Token": "nope"}
        )
        assert wrong.status_code == 401
        right = client.post(
            "/sse/notify", json=body, headers={"X-Keep-Notify-Token": "s3cret"}
        )
        assert right.status_code == 204


def test_the_notify_route_stays_open_when_no_token_is_configured(monkeypatch):
    from fastapi.testclient import TestClient

    from src.routes import sse_routes

    _configure(monkeypatch, None, "none")
    monkeypatch.setattr(sse_routes, "SSE_NOTIFY_TOKEN", None)
    body = {"tenant_id": "t1", "event": "poll-alerts", "data": {}}
    with TestClient(_notify_app()) as client:
        assert client.post("/sse/notify", json=body).status_code == 204


def test_a_malformed_message_does_not_drop_the_subscription(monkeypatch):
    """One bad message on the shared channel must be skipped, not treated as
    Redis being lost: tearing the subscription down loses every message
    published while it reconnects."""

    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        await sse_module.start_fanout()
        await asyncio.sleep(0.1)
        local_stream = sse_module.sse_broadcaster.subscribe("t1")
        await local_stream.__anext__()
        subscribe_errors = _counter(
            metrics.sse_fanout_errors_total, operation="subscribe"
        )
        schema_errors = _counter(metrics.sse_fanout_errors_total, operation="schema")
        rogue = server.client()
        await rogue.publish("keep:sse", json.dumps({"origin": "elsewhere"}))
        await rogue.publish(
            "keep:sse",
            json.dumps(
                {
                    "origin": "elsewhere",
                    "tenant_id": "t1",
                    "event": "poll-alerts",
                    "data": {},
                }
            ),
        )
        local = await _next_or_none(local_stream, 0.5)
        await local_stream.aclose()
        await sse_module.stop_fanout()
        return (
            local,
            _counter(metrics.sse_fanout_errors_total, operation="subscribe")
            - subscribe_errors,
            _counter(metrics.sse_fanout_errors_total, operation="schema")
            - schema_errors,
        )

    local, subscribe_delta, schema_delta = asyncio.run(run())
    assert local is not None and local.startswith("event: poll-alerts\n")
    assert (subscribe_delta, schema_delta) == (0, 1)


def test_a_delivery_failure_does_not_drop_the_subscription(monkeypatch):
    """Whatever goes wrong while handing one message to the local broadcaster
    must not be mistaken for Redis being lost."""

    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        await sse_module.start_fanout()
        await asyncio.sleep(0.1)
        broadcaster = sse_module.sse_broadcaster
        real_notify = broadcaster.notify
        seen = []

        async def flaky_notify(tenant_id, event, data):
            seen.append(event)
            if len(seen) == 1:
                raise RuntimeError("boom")
            await real_notify(tenant_id, event, data)

        monkeypatch.setattr(broadcaster, "notify", flaky_notify)
        local_stream = broadcaster.subscribe("t1")
        await local_stream.__anext__()
        subscribe_errors = _counter(
            metrics.sse_fanout_errors_total, operation="subscribe"
        )
        deliver_errors = _counter(metrics.sse_fanout_errors_total, operation="deliver")
        rogue = server.client()
        for event in ("poll-alerts", "incident-change"):
            await rogue.publish(
                "keep:sse",
                json.dumps(
                    {
                        "origin": "elsewhere",
                        "tenant_id": "t1",
                        "event": event,
                        "data": {},
                    }
                ),
            )
        local = await _next_or_none(local_stream, 0.5)
        await local_stream.aclose()
        await sse_module.stop_fanout()
        return (
            local,
            _counter(metrics.sse_fanout_errors_total, operation="subscribe")
            - subscribe_errors,
            _counter(metrics.sse_fanout_errors_total, operation="deliver")
            - deliver_errors,
        )

    local, subscribe_delta, deliver_delta = asyncio.run(run())
    assert local is not None and local.startswith("event: incident-change\n")
    assert (subscribe_delta, deliver_delta) == (0, 1)


def test_an_open_notify_route_is_flagged_when_the_fan_out_starts(monkeypatch, caplog):
    """With the fan-out on, an unguarded notify route reaches every gateway
    process; starting that way must be visible."""

    async def run():
        server = FakePubSubServer()
        _configure(monkeypatch, server, "redis")
        monkeypatch.setattr(sse_module, "SSE_NOTIFY_TOKEN", None, raising=False)
        with caplog.at_level("WARNING", logger="src.services.sse"):
            await sse_module.start_fanout()
        await sse_module.stop_fanout()

    asyncio.run(run())
    assert any("SSE_NOTIFY_TOKEN" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_the_notify_route_rejects_a_wrong_token_when_the_configured_one_is_not_ascii(
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from src.routes import sse_routes

    _configure(monkeypatch, None, "none")
    monkeypatch.setattr(sse_routes, "SSE_NOTIFY_TOKEN", "s\u00e9cret")
    body = {"tenant_id": "t1", "event": "poll-alerts", "data": {}}
    with TestClient(_notify_app()) as client:
        wrong = client.post(
            "/sse/notify", json=body, headers={"X-Keep-Notify-Token": "nope"}
        )
        assert wrong.status_code == 401


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
    assert kwargs["health_check_interval"] == 30


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
    assert pool.connection_kwargs["health_check_interval"] == 30
    assert [
        (
            s.connection_pool.connection_kwargs["host"],
            s.connection_pool.connection_kwargs["port"],
        )
        for s in pool.sentinel_manager.sentinels
    ] == [("s1", 26379), ("s2", 26379)]


@pytest.mark.skipif(
    not os.environ.get("SSE_FANOUT_TEST_REDIS_URL"), reason="needs a real Redis"
)
def test_notify_sse_from_a_worker_thread_over_a_real_redis(monkeypatch, caplog):
    import redis.asyncio as aioredis

    url = os.environ["SSE_FANOUT_TEST_REDIS_URL"]

    async def run():
        monkeypatch.setattr(sse_module, "SSE_FANOUT", "redis")
        monkeypatch.setattr(sse_module, "SSE_FANOUT_CHANNEL", "keep:sse-test-thread")
        monkeypatch.setattr(sse_module, "sse_broadcaster", sse_module.SSEBroadcaster())
        monkeypatch.setattr(
            sse_module,
            "redis_client_factory",
            lambda **s: (lambda: aioredis.from_url(url)),
        )
        await sse_module.start_fanout()
        await asyncio.sleep(0.3)
        broker_b = sse_module.SSEBroadcaster()
        fanout_b = RedisFanout(
            broker_b,
            lambda: aioredis.from_url(url),
            channel="keep:sse-test-thread",
            origin="b",
        )
        await fanout_b.start()
        await asyncio.sleep(0.3)
        stream_b = broker_b.subscribe("t1")
        await stream_b.__anext__()
        errors_before = _counter(metrics.sse_fanout_errors_total, operation="publish")
        await asyncio.get_running_loop().run_in_executor(
            None, sse_module.notify_sse, "t1", "poll-alerts", {"thread": True}
        )
        event = await _next_or_none(stream_b, 3)
        await stream_b.aclose()
        await fanout_b.stop()
        await sse_module.stop_fanout()
        return (
            event,
            _counter(metrics.sse_fanout_errors_total, operation="publish")
            - errors_before,
        )

    with caplog.at_level("WARNING", logger="src.services.sse_fanout"):
        event, errors = asyncio.run(run())
    failures = [
        (record.getMessage(), record.__dict__.get("error"), record.exc_text)
        for record in caplog.records
        if record.name == "src.services.sse_fanout"
    ]
    assert event is not None and event.startswith("event: poll-alerts\n")
    assert errors == 0 and not failures, failures
