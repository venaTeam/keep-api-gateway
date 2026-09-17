"""SSE streams must end when the process is told to stop.

A stream never ends on its own, so a graceful shutdown that waits for open
responses waits forever: gunicorn force-kills the worker after 30 s and the
browser sits on a zombie stream that still sends keepalives while a fresh
process receives every notification. On SIGTERM the broadcaster closes every
subscriber queue so the responses end at once, the drain completes and the
normal shutdown runs. The `connected` event announces the keepalive interval
so the client's stale threshold follows the server's configuration, and the
broadcaster counts what it delivers and what it drops.
"""

import asyncio
import signal

from src.repositories import metrics
from src.services import sse as sse_module


def _counter(counter, **labels):
    return counter.labels(**labels)._value.get()


def test_close_all_ends_open_streams_without_waiting_for_the_keepalive(monkeypatch):
    monkeypatch.setattr(sse_module, "SSE_KEEPALIVE_INTERVAL_SECONDS", 30)

    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        stream = broadcaster.subscribe("t1")
        await stream.__anext__()
        started = asyncio.get_running_loop().time()
        broadcaster.close_all()
        try:
            await stream.__anext__()
            ended = False
        except StopAsyncIteration:
            ended = True
        return ended, asyncio.get_running_loop().time() - started

    ended, elapsed = asyncio.run(run())
    assert ended
    assert elapsed < 1.0


def test_subscribe_after_close_all_still_delivers():
    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        first = broadcaster.subscribe("t1")
        await first.__anext__()
        broadcaster.close_all()
        second = broadcaster.subscribe("t1")
        await second.__anext__()
        await broadcaster.notify("t1", "poll-alerts", {})
        event = await second.__anext__()
        await second.aclose()
        return event

    assert asyncio.run(run()).startswith("event: poll-alerts\n")


def test_connected_event_announces_the_keepalive_interval(monkeypatch):
    monkeypatch.setattr(sse_module, "SSE_KEEPALIVE_INTERVAL_SECONDS", 7)

    async def run():
        stream = sse_module.SSEBroadcaster().subscribe("t1")
        first = await stream.__anext__()
        await stream.aclose()
        return first

    assert '"keepalive_seconds": 7' in asyncio.run(run())


def test_shutdown_signal_closes_streams_then_chains_the_previous_handler():
    previous_calls = []

    def previous(signum, frame):
        previous_calls.append(signum)

    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        stream = broadcaster.subscribe("t1")
        await stream.__anext__()
        handler = sse_module.close_streams_on_signal(
            previous, asyncio.get_running_loop(), broadcaster
        )
        handler(signal.SIGTERM, None)
        try:
            await asyncio.wait_for(stream.__anext__(), timeout=1)
            return False
        except StopAsyncIteration:
            return True

    assert asyncio.run(run()) is True
    assert previous_calls == [signal.SIGTERM]


def test_notify_counts_delivered_and_dropped_notifications():
    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        before_dropped = _counter(
            metrics.sse_notifications_total,
            event="poll-alerts",
            outcome="no_subscriber",
        )
        before_delivered = _counter(
            metrics.sse_notifications_total, event="poll-alerts", outcome="delivered"
        )
        await broadcaster.notify("t1", "poll-alerts", {})
        stream = broadcaster.subscribe("t1")
        await stream.__anext__()
        await broadcaster.notify("t1", "poll-alerts", {})
        await stream.aclose()
        return (
            _counter(
                metrics.sse_notifications_total,
                event="poll-alerts",
                outcome="no_subscriber",
            )
            - before_dropped,
            _counter(
                metrics.sse_notifications_total,
                event="poll-alerts",
                outcome="delivered",
            )
            - before_delivered,
        )

    assert asyncio.run(run()) == (1, 1)


def test_stream_closures_are_counted_by_reason():
    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        before_client = _counter(metrics.sse_streams_closed_total, reason="client")
        before_shutdown = _counter(metrics.sse_streams_closed_total, reason="shutdown")
        first = broadcaster.subscribe("t1")
        await first.__anext__()
        await first.aclose()
        second = broadcaster.subscribe("t1")
        await second.__anext__()
        broadcaster.close_all()
        try:
            await second.__anext__()
        except StopAsyncIteration:
            pass
        return (
            _counter(metrics.sse_streams_closed_total, reason="client") - before_client,
            _counter(metrics.sse_streams_closed_total, reason="shutdown")
            - before_shutdown,
        )

    assert asyncio.run(run()) == (1, 1)


def test_install_shutdown_handlers_survives_earlier_lifespans_on_closed_loops():
    """Every lifespan installs the handlers again, and the earlier installs are
    bound to loops that have since been closed. The signal must still close
    the live streams and reach the handler that was there before Keep's,
    instead of raising from a dead loop and leaving the server undrained."""
    previous_calls = []

    def previous(signum, frame):
        previous_calls.append(signum)

    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in originals:
            signal.signal(sig, previous)
        for _ in range(3):
            stale = asyncio.new_event_loop()
            sse_module.install_shutdown_handlers(stale)
            stale.close()

        async def run():
            sse_module.install_shutdown_handlers(asyncio.get_running_loop())
            stream = sse_module.sse_broadcaster.subscribe("t1")
            await stream.__anext__()
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            try:
                await asyncio.wait_for(stream.__anext__(), timeout=1)
                return False
            except StopAsyncIteration:
                return True

        assert asyncio.run(run()) is True
        assert previous_calls == [signal.SIGTERM]
        assert signal.getsignal(signal.SIGTERM)._keep_sse_previous is previous
    finally:
        for sig, handler in originals.items():
            signal.signal(sig, handler)


def test_notify_counts_unknown_event_names_under_a_fixed_label():
    """The event label reaches Prometheus straight from the notify route, so
    an arbitrary name must not create a new series."""

    async def run():
        broadcaster = sse_module.SSEBroadcaster()
        before = _counter(
            metrics.sse_notifications_total, event="other", outcome="no_subscriber"
        )
        await broadcaster.notify("t1", "made-up-event-4711", {})
        return (
            _counter(
                metrics.sse_notifications_total, event="other", outcome="no_subscriber"
            )
            - before
        )

    assert asyncio.run(run()) == 1
    assert not any(
        "made-up-event-4711" in key for key in metrics.sse_notifications_total._metrics
    )
