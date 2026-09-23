"""SSE keepalive pacing (`SSE_KEEPALIVE_INTERVAL_SECONDS`).

An idle stream has to put a byte on the wire before any proxy on the path hits
its idle timeout -- the OpenShift router's `timeout server` defaults to 30 s --
so the default interval must sit well below 30 s, the configured interval must
be what actually paces the keepalives, and a real event must never wait for
the keepalive timer.
"""

import asyncio

from src.config.config import SSE_KEEPALIVE_INTERVAL_SECONDS
from src.services import sse as sse_module

KEEPALIVE = ": keepalive\n\n"


def test_default_interval_is_well_below_the_router_idle_timeout():
    assert SSE_KEEPALIVE_INTERVAL_SECONDS == 15
    assert SSE_KEEPALIVE_INTERVAL_SECONDS < 30


def test_idle_stream_emits_keepalives_at_the_configured_interval(monkeypatch):
    monkeypatch.setattr(sse_module, "SSE_KEEPALIVE_INTERVAL_SECONDS", 0.05)

    async def collect():
        stream = sse_module.SSEBroadcaster().subscribe("t1")
        first = await stream.__anext__()
        started = asyncio.get_running_loop().time()
        second = await stream.__anext__()
        third = await stream.__anext__()
        elapsed = asyncio.get_running_loop().time() - started
        await stream.aclose()
        return first, second, third, elapsed

    first, second, third, elapsed = asyncio.run(collect())
    assert first.startswith("event: connected\n")
    assert second == KEEPALIVE
    assert third == KEEPALIVE
    assert 0.08 <= elapsed < 1.0


def test_event_is_delivered_without_waiting_for_the_keepalive_timer(monkeypatch):
    monkeypatch.setattr(sse_module, "SSE_KEEPALIVE_INTERVAL_SECONDS", 30)

    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        stream = broadcaster.subscribe("t1")
        await stream.__anext__()
        started = asyncio.get_running_loop().time()
        await broadcaster.notify("t1", "poll-alerts", {"alerts": []})
        event = await stream.__anext__()
        elapsed = asyncio.get_running_loop().time() - started
        await stream.aclose()
        return event, elapsed

    event, elapsed = asyncio.run(run())
    assert event.startswith("event: poll-alerts\n")
    assert elapsed < 1.0
