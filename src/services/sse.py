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
"""

import asyncio
import json
import logging
import signal
import threading
from typing import Any, AsyncGenerator, Callable, Dict, List

from src.config.config import SSE_KEEPALIVE_INTERVAL_SECONDS
from src.repositories.metrics import (
    connected_users_gauge,
    sse_notifications_total,
    sse_streams_closed_total,
)

logger = logging.getLogger(__name__)

_CLOSE = object()

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
        """
        async with self._lock:
            connections = self._connections.get(tenant_id, [])
            if not connections:
                sse_notifications_total.labels(
                    event=event, outcome="no_subscriber"
                ).inc()
                logger.debug(
                    "No SSE connections for tenant, skipping notification",
                    extra={"tenant_id": tenant_id, "event": event}
                )
                return
            sse_notifications_total.labels(event=event, outcome="delivered").inc()

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

def notify_sse(tenant_id: str, event: str, data: Any) -> None:
    """
    Synchronous wrapper to send SSE notifications.
    
    This function can be called from synchronous code and will
    schedule the notification in the event loop.
    
    Args:
        tenant_id: The tenant ID to notify
        event: The event name/type
        data: The event data
    """
    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(
            sse_broadcaster.notify(tenant_id, event, data),
            loop
        )
    except RuntimeError:
        try:
            asyncio.run(sse_broadcaster.notify(tenant_id, event, data))
        except Exception as e:
            logger.warning(
                "Failed to send SSE notification (no event loop)",
                extra={"tenant_id": tenant_id, "event": event, "error": str(e)}
            )
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
        loop.call_soon_threadsafe(broadcaster.close_all)
        if callable(previous):
            previous(signum, frame)

    return handler


def install_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """
    Wrap the process's SIGTERM and SIGINT handlers so streams close before the
    server drains. Must run on the main thread, which is where uvicorn has
    already installed its own handlers by the time the lifespan starts.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning("Not on the main thread; SSE streams will not close on shutdown")
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        signal.signal(sig, close_streams_on_signal(previous, loop, sse_broadcaster))
