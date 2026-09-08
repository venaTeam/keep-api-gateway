"""
Server-Sent Events (SSE) broadcaster for real-time notifications.

This module provides an in-memory SSE broadcaster that maintains per-tenant
connection queues and broadcasts events to all connected clients.

An idle stream emits a `: keepalive` comment every
`SSE_KEEPALIVE_INTERVAL_SECONDS` (default 15 s). Every proxy between the
browser and this process applies an idle timeout to the response — the
OpenShift router's `timeout server` defaults to 30 s — and closes the stream
when no byte crosses it in time, so the interval must stay well below the
smallest such timeout on the path. Keep the two values apart: at 30 s / 30 s
the router wins the race and every idle stream reconnects each half minute.

A stream never ends on its own, so on SIGTERM the broadcaster closes every
subscriber queue: the responses end at once, the server's drain completes and
the lifespan shutdown runs instead of the worker being force-killed after
gunicorn's graceful timeout while browsers sit on a zombie stream.

The broadcaster only knows this process's streams. With `SSE_FANOUT=redis`,
`broadcast()` also publishes every notification through
`src.services.sse_fanout` so the replicas holding the other streams deliver it
too; without it, `broadcast()` is local delivery, exactly as before.
"""

import asyncio
import json
import logging
import signal
import threading
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from src.config.config import (
    REDIS_DB,
    REDIS_HOST,
    REDIS_KEY_PREFIX,
    REDIS_PASSWORD,
    REDIS_PORT,
    REDIS_SENTINEL_ENABLED,
    REDIS_SENTINEL_HOSTS,
    REDIS_SENTINEL_SERVICE_NAME,
    REDIS_SSL,
    REDIS_USERNAME,
    SSE_FANOUT,
    SSE_FANOUT_CHANNEL,
    SSE_KEEPALIVE_INTERVAL_SECONDS,
)
from src.repositories.metrics import (
    connected_users_gauge,
    sse_notifications_total,
    sse_streams_closed_total,
)
from src.services.sse_fanout import RedisFanout, redis_client_factory

logger = logging.getLogger(__name__)

_CLOSE = object()

SSE_EVENTS = frozenset(
    {
        "connected",
        "poll-alerts",
        "incident-change",
        "incident-comment",
        "poll-presets",
        "topology-update",
        "ai-logs-change",
        "alert-update",
    }
)

class SSEBroadcaster:
    """
    In-memory SSE broadcaster that manages connections per tenant.
    
    Each tenant can have multiple connections (browser tabs, etc.),
    and events are broadcast to all connections for that tenant.
    """

    def __init__(self):
        self._connections: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, tenant_id: str) -> AsyncGenerator[str, None]:
        """
        Subscribe to SSE events for a tenant.
        
        Creates a new queue for this connection and yields SSE-formatted
        events as they arrive.
        
        Args:
            tenant_id: The tenant ID to subscribe to
            
        Yields:
            SSE-formatted event strings, plus a `: keepalive` comment whenever
            no event arrived for `SSE_KEEPALIVE_INTERVAL_SECONDS`, so idle
            streams outlive the proxies' idle timeouts. The first event is
            `connected` and announces that interval so the client can size its
            liveness watchdog. The stream ends when `close_all()` is called.
        """
        queue: asyncio.Queue = asyncio.Queue()

        async with self._lock:
            if tenant_id not in self._connections:
                self._connections[tenant_id] = []
            self._connections[tenant_id].append(queue)
            logger.info(
                "SSE client subscribed",
                extra={
                    "tenant_id": tenant_id,
                    "total_connections": len(self._connections[tenant_id])
                }
            )
        try:
            connected_users_gauge.labels(tenant_id=tenant_id).inc()
        except Exception:
            logger.debug("Failed to increment connected_users gauge", exc_info=True)

        close_reason = "client"
        try:
            yield self._format_sse(
                "connected",
                {
                    "status": "connected",
                    "keepalive_seconds": SSE_KEEPALIVE_INTERVAL_SECONDS,
                },
            )

            while True:
                try:
                    event_data = await asyncio.wait_for(
                        queue.get(), timeout=SSE_KEEPALIVE_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event_data is _CLOSE:
                    close_reason = "shutdown"
                    return
                yield event_data
        except asyncio.CancelledError:
            logger.debug(f"SSE connection cancelled for tenant {tenant_id}")
            raise
        except Exception:
            close_reason = "error"
            raise
        finally:
            try:
                connected_users_gauge.labels(tenant_id=tenant_id).dec()
                sse_streams_closed_total.labels(reason=close_reason).inc()
            except Exception:
                logger.debug("Failed to update SSE stream metrics", exc_info=True)
            async with self._lock:
                if tenant_id in self._connections:
                    try:
                        self._connections[tenant_id].remove(queue)
                        if not self._connections[tenant_id]:
                            del self._connections[tenant_id]
                        logger.info(
                            "SSE client disconnected",
                            extra={
                                "tenant_id": tenant_id,
                                "remaining_connections": len(self._connections.get(tenant_id, []))
                            }
                        )
                    except ValueError:
                        pass

    async def notify(self, tenant_id: str, event: str, data: Any) -> None:
        """
        Send an event to all connected clients for a tenant.
        
        Args:
            tenant_id: The tenant ID to notify
            event: The event name/type
            data: The event data (will be JSON serialized)

        The event label on the metric is limited to the stream protocol's
        event names; any other name is counted as "other", since the name
        arrives straight from the notify request and must not add series.
        """
        metric_event = event if event in SSE_EVENTS else "other"
        async with self._lock:
            connections = self._connections.get(tenant_id, [])
            if not connections:
                sse_notifications_total.labels(
                    event=metric_event, outcome="no_subscriber"
                ).inc()
                logger.debug(
                    "No SSE connections for tenant, skipping notification",
                    extra={"tenant_id": tenant_id, "event": event}
                )
                return
            sse_notifications_total.labels(
                event=metric_event, outcome="delivered"
            ).inc()

            sse_message = self._format_sse(event, data)

            for queue in connections:
                try:
                    queue.put_nowait(sse_message)
                except asyncio.QueueFull:
                    logger.warning(
                        "SSE queue full for tenant",
                        extra={"tenant_id": tenant_id, "event": event}
                    )

            logger.debug(
                "SSE event broadcast",
                extra={
                    "tenant_id": tenant_id,
                    "event": event,
                    "connections": len(connections)
                }
            )

    def close_all(self) -> None:
        """
        End every open stream at once.

        Runs on the event loop thread and only enqueues a close marker per
        subscriber; each `subscribe()` generator returns when it dequeues it,
        which ends its `StreamingResponse` so a graceful shutdown can finish
        instead of waiting on streams that would otherwise never end.
        """
        queues = [queue for queues in self._connections.values() for queue in queues]
        for queue in queues:
            queue.put_nowait(_CLOSE)
        logger.info("Closing SSE streams for shutdown", extra={"streams": len(queues)})

    def _format_sse(self, event: str, data: Any) -> str:
        """
        Format data as an SSE message.
        
        Args:
            event: The event name
            data: The event data
            
        Returns:
            SSE-formatted string
        """
        json_data = json.dumps(data, default=str)
        return f"event: {event}\ndata: {json_data}\n\n"

sse_broadcaster = SSEBroadcaster()

_fanout: Optional[RedisFanout] = None
_server_loop: Optional[asyncio.AbstractEventLoop] = None

async def start_fanout() -> None:
    """
    Connect this process to the cross-process fan-out when `SSE_FANOUT=redis`.

    Runs from the lifespan startup. Never raises: an unreachable Redis is
    logged and retried by the fan-out's subscriber, and publishing degrades to
    local delivery until it is back.

    On a Redis shared with other applications the clients select `REDIS_DB`
    and the channel carries `REDIS_KEY_PREFIX`; the prefix is what isolates
    the channel, since pub/sub channels are instance-wide whatever the
    database index.
    """
    global _fanout, _server_loop
    _server_loop = asyncio.get_running_loop()
    if SSE_FANOUT != "redis":
        return
    client_factory = redis_client_factory(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        ssl=REDIS_SSL,
        username=REDIS_USERNAME,
        password=REDIS_PASSWORD,
        sentinel_hosts=REDIS_SENTINEL_HOSTS if REDIS_SENTINEL_ENABLED else None,
        sentinel_service=REDIS_SENTINEL_SERVICE_NAME,
    )
    channel = f"{REDIS_KEY_PREFIX}{SSE_FANOUT_CHANNEL}"
    _fanout = RedisFanout(sse_broadcaster, client_factory, channel=channel)
    await _fanout.start()
    logger.info("SSE fan-out enabled", extra={"channel": channel})

async def stop_fanout() -> None:
    """Disconnect from the fan-out; later broadcasts deliver locally only."""
    global _fanout, _server_loop
    fanout, _fanout = _fanout, None
    _server_loop = None
    if fanout is not None:
        await fanout.stop()

async def broadcast(tenant_id: str, event: str, data: Any) -> None:
    """
    Notify a tenant's subscribers in every gateway process.

    Delivers to this process's streams and, when the fan-out is enabled, to the
    streams held by the other replicas. This is the entry point every notifier
    must use; `SSEBroadcaster.notify()` alone only reaches the local process.
    """
    if _fanout is None:
        await sse_broadcaster.notify(tenant_id, event, data)
    else:
        await _fanout.broadcast(tenant_id, event, data)

def notify_sse(tenant_id: str, event: str, data: Any) -> None:
    """
    Synchronous wrapper to send SSE notifications.

    Callable from synchronous code, including route handlers that FastAPI runs
    in worker threads. The notification is scheduled on the server's event
    loop, where the broadcaster and the fan-out's Redis client live; running
    it on a throwaway loop would leave the local subscribers served but make
    every publish fail with "attached to a different loop". The scheduled
    future is not awaited: delivery and publish failures are handled inside
    `broadcast()`, so the warning below only covers a failure to schedule.

    When no server loop is running (scripts, tests, a lifespan that has
    already ended) the notification goes to the local broadcaster on a private
    loop. That path never touches the fan-out, so it cannot repeat the
    different-loop failure, and it is logged because it silently masked that
    failure once.

    Args:
        tenant_id: The tenant ID to notify
        event: The event name/type
        data: The event data
    """
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = (
                _server_loop
                if _server_loop is not None and _server_loop.is_running()
                else None
            )
        if loop is not None:
            asyncio.run_coroutine_threadsafe(broadcast(tenant_id, event, data), loop)
        else:
            logger.warning(
                "No running server loop; SSE notification delivered locally only",
                extra={"tenant_id": tenant_id, "event": event},
            )
            asyncio.run(sse_broadcaster.notify(tenant_id, event, data))
    except Exception as e:
        logger.warning(
            "Failed to send SSE notification",
            extra={"tenant_id": tenant_id, "event": event, "error": str(e)}
        )


def close_streams_on_signal(
    previous: Any, loop: asyncio.AbstractEventLoop, broadcaster: SSEBroadcaster
) -> Callable[[int, Any], None]:
    """
    Build a signal handler that closes every SSE stream, then defers to the
    handler that was installed before it (uvicorn's, which starts the drain).

    Closing the streams first is what lets the drain complete: uvicorn only
    reaches the lifespan shutdown once every response has ended, and an SSE
    response never ends on its own.
    """

    def handler(signum: int, frame: Any) -> None:
        try:
            loop.call_soon_threadsafe(broadcaster.close_all)
        except RuntimeError:
            logger.debug("SSE streams not closed on signal: the server loop is gone")
        if callable(previous):
            previous(signum, frame)

    handler._keep_sse_previous = previous
    return handler


def install_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """
    Wrap the process's SIGTERM and SIGINT handlers so streams close before the
    server drains. Must run on the main thread, which is where uvicorn has
    already installed its own handlers by the time the lifespan starts.

    Installing again replaces an earlier Keep handler instead of stacking on
    it, so a lifespan that starts after another has ended never chains
    through a loop that is already closed.
    """
    global _server_loop
    _server_loop = loop
    if threading.current_thread() is not threading.main_thread():
        logger.warning("Not on the main thread; SSE streams will not close on shutdown")
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        previous = getattr(previous, "_keep_sse_previous", previous)
        signal.signal(sig, close_streams_on_signal(previous, loop, sse_broadcaster))
