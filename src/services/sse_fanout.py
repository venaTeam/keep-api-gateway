"""
Cross-process fan-out for SSE notifications over Redis/Valkey pub/sub.

The broadcaster in `src.services.sse` is per process: a notification that
reaches one gateway replica is never seen by a browser whose stream lives in
another. `RedisFanout.broadcast()` publishes the notification on one shared channel
and every process, the publishing one included, delivers what arrives on its
subscription. Redis hands every subscriber the channel's messages in publish
order, so all processes deliver notifications in the same order and a local
delivery can never overtake an older notification still in flight.

Redis being unavailable, a publish that does not answer in time, or this
process's subscription being down degrades to today's behaviour — local
delivery — with a warning and a counter, never an error; the subscriber
reconnects with a capped backoff and never affects readiness.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Optional

from src.repositories.metrics import (
    sse_fanout_connected,
    sse_fanout_errors_total,
    sse_fanout_published_total,
    sse_fanout_received_total,
)

logger = logging.getLogger(__name__)

REDIS_HEALTH_CHECK_SECONDS = 30
PUBLISH_TIMEOUT_SECONDS = 2.0
PING_INTERVAL_SECONDS = 15.0
PONG_TIMEOUT_SECONDS = 5.0

REFETCH_EVENTS = frozenset({"poll-alerts", "incident-change", "incident-comment"})


class RedisFanout:
    """
    One fan-out per process. `client_factory` returns a `redis.asyncio`
    compatible client (`publish`, `pubsub()`, `aclose`); it is called again
    after every failure so a fresh connection is used on reconnect.

    A publish is abandoned after `publish_timeout`. The subscription is pinged
    once it has been quiet for `ping_interval` and rebuilt when nothing, the
    PONG included, arrives within `pong_timeout` after that.
    """

    def __init__(
        self,
        broadcaster: Any,
        client_factory: Callable[[], Any],
        channel: str = "keep:sse",
        origin: Optional[str] = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        publish_timeout: float = PUBLISH_TIMEOUT_SECONDS,
        ping_interval: float = PING_INTERVAL_SECONDS,
        pong_timeout: float = PONG_TIMEOUT_SECONDS,
    ):
        self._broadcaster = broadcaster
        self._client_factory = client_factory
        self._channel = channel
        self._origin = origin or uuid.uuid4().hex
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._publish_timeout = publish_timeout
        self._ping_interval = ping_interval
        self._pong_timeout = pong_timeout
        self._subscribed = False
        self._subscription = 0
        self._publisher: Any = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    async def start(self) -> None:
        """Start the subscriber loop for this process."""
        self._stopping = False
        self._task = asyncio.create_task(self._subscribe_loop())

    async def stop(self) -> None:
        """Stop the subscriber loop and close the publisher connection."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._publisher is not None:
            try:
                await self._publisher.aclose()
            except Exception:
                logger.debug("Failed to close the fan-out publisher", exc_info=True)
            self._publisher = None

    async def broadcast(self, tenant_id: str, event: str, data: Any) -> None:
        """
        Publish to every process. This process's subscribers get the
        notification back through its own subscription, in channel order.

        When that is not certain (the publish failed or timed out, or the
        subscription was not acknowledged or was replaced while publishing)
        they are told directly. The notification may still arrive through the
        channel, before or after newer ones, so a payload replayed here could
        overwrite newer state: events the views answer by refetching are
        delivered with an empty payload, which only asks them to refetch.
        """
        subscription = self._subscription if self._subscribed else None
        published = await self._publish(tenant_id, event, data)
        if (
            published
            and subscription is not None
            and self._subscribed
            and self._subscription == subscription
        ):
            return
        await self._broadcaster.notify(
            tenant_id, event, {} if event in REFETCH_EVENTS else data
        )

    async def _publish(self, tenant_id: str, event: str, data: Any) -> bool:
        message = json.dumps(
            {
                "origin": self._origin,
                "tenant_id": tenant_id,
                "event": event,
                "data": data,
            },
            default=str,
        )
        try:
            if self._publisher is None:
                self._publisher = self._client_factory()
            await asyncio.wait_for(
                self._publisher.publish(self._channel, message),
                timeout=self._publish_timeout,
            )
            sse_fanout_published_total.inc()
            return True
        except Exception as e:
            self._publisher = None
            sse_fanout_errors_total.labels(operation="publish").inc()
            logger.warning(
                "SSE fan-out publish failed; delivered locally only",
                extra={
                    "tenant_id": tenant_id,
                    "event": event,
                    "error": str(e) or type(e).__name__,
                },
            )
            return False

    async def _subscribe_loop(self) -> None:
        delay = self._reconnect_delay
        while not self._stopping:
            client = None
            pubsub = None
            try:
                client = self._client_factory()
                pubsub = client.pubsub()
                await pubsub.subscribe(self._channel)
                await self._await_acknowledgement(pubsub)
                self._subscription += 1
                self._subscribed = True
                sse_fanout_connected.set(1)
                logger.info("SSE fan-out subscribed", extra={"channel": self._channel})
                delay = self._reconnect_delay
                loop = asyncio.get_running_loop()
                last_heard = loop.time()
                pinged = False
                while not self._stopping:
                    quiet = loop.time() - last_heard
                    if quiet > self._ping_interval + self._pong_timeout:
                        raise TimeoutError("no PONG from Redis")
                    if quiet > self._ping_interval and not pinged:
                        await asyncio.wait_for(
                            pubsub.ping(), timeout=self._pong_timeout
                        )
                        pinged = True
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=min(1.0, self._ping_interval),
                    )
                    if message is not None:
                        last_heard = loop.time()
                        pinged = False
                    if message is not None and message.get("type") == "message":
                        try:
                            await self._deliver(message)
                        except Exception:
                            sse_fanout_errors_total.labels(operation="deliver").inc()
                            logger.warning(
                                "SSE fan-out failed to deliver a message", exc_info=True
                            )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._subscribed = False
                sse_fanout_connected.set(0)
                sse_fanout_errors_total.labels(operation="subscribe").inc()
                logger.warning(
                    "SSE fan-out subscriber lost Redis; retrying",
                    extra={
                        "error": str(e) or type(e).__name__,
                        "retry_in_seconds": delay,
                    },
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
            finally:
                self._subscribed = False
                for closeable in (pubsub, client):
                    if closeable is not None:
                        try:
                            await closeable.aclose()
                        except Exception:
                            logger.debug(
                                "Failed to close a fan-out connection", exc_info=True
                            )
        sse_fanout_connected.set(0)

    async def _await_acknowledgement(self, pubsub: Any) -> None:
        """
        redis-py returns from `subscribe()` once SUBSCRIBE is sent; the
        subscription only exists once Redis acknowledges it, and a publish
        before that reaches nobody here.
        """
        deadline = asyncio.get_running_loop().time() + self._pong_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Redis did not acknowledge the subscription")
            message = await pubsub.get_message(
                ignore_subscribe_messages=False, timeout=min(1.0, remaining)
            )
            if message is not None and message.get("type") == "subscribe":
                return

    async def _deliver(self, message: Any) -> None:
        try:
            payload = json.loads(message["data"])
        except Exception:
            sse_fanout_errors_total.labels(operation="decode").inc()
            logger.warning("SSE fan-out received an undecodable message")
            return
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("tenant_id"), str)
            or not isinstance(payload.get("event"), str)
        ):
            sse_fanout_errors_total.labels(operation="schema").inc()
            logger.warning("SSE fan-out received a malformed message")
            return
        if payload.get("origin") != self._origin:
            sse_fanout_received_total.inc()
        await self._broadcaster.notify(
            payload["tenant_id"], payload["event"], payload.get("data", {})
        )


def redis_client_factory(
    host: str,
    port: int,
    username: Optional[str],
    password: Optional[str],
    sentinel_hosts: Optional[str],
    sentinel_service: str,
    db: int = 0,
    ssl: bool = False,
    sentinel_username: Optional[str] = None,
    sentinel_password: Optional[str] = None,
) -> Callable[[], Any]:
    """
    Build the client factory from the same settings keep-workflows uses for
    its arq pool: direct `REDIS_HOST`/`REDIS_PORT`, or Sentinel when
    `REDIS_SENTINEL_HOSTS` is set. `db` selects the logical database and
    `ssl` enables TLS on the data connections (with Sentinel, on the master
    connections it hands out). Every connection pings after
    `REDIS_HEALTH_CHECK_SECONDS` of silence, so a half-open pub/sub socket is
    noticed and re-subscribed instead of waiting for the TCP keepalive.
    Sentinels that require AUTH get `sentinel_username`/`sentinel_password`,
    which may differ from the master's credentials; without them the
    Sentinels are contacted unauthenticated.
    """
    import redis.asyncio as aioredis

    if sentinel_hosts:
        from redis.asyncio.sentinel import Sentinel

        nodes = []
        for host_port in sentinel_hosts.split(","):
            host_port = host_port.strip()
            node_host, _, node_port = host_port.partition(":")
            nodes.append((node_host, int(node_port or 26379)))

        def from_sentinel() -> Any:
            sentinel_kwargs: dict = {"socket_timeout": 5}
            if sentinel_username:
                sentinel_kwargs["username"] = sentinel_username
            if sentinel_password:
                sentinel_kwargs["password"] = sentinel_password
            sentinel = Sentinel(
                nodes,
                sentinel_kwargs=sentinel_kwargs,
                username=username,
                password=password,
                socket_timeout=5,
            )
            return sentinel.master_for(
                sentinel_service,
                username=username,
                password=password,
                db=db,
                ssl=ssl,
                health_check_interval=REDIS_HEALTH_CHECK_SECONDS,
            )

        return from_sentinel

    def direct() -> Any:
        return aioredis.Redis(
            host=host,
            port=port,
            db=db,
            ssl=ssl,
            username=username,
            password=password,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=REDIS_HEALTH_CHECK_SECONDS,
        )

    return direct
