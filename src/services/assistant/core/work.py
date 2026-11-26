"""Work that outlives the request it came from, and what becomes of it on shutdown.

A worker that gets SIGTERM has 30 s (gunicorn's graceful timeout). It first waits for
its open responses, and only then runs the lifespan's shutdown, which can take 20 s to
stop its consumer threads. So nothing here may hold a response open for longer than
SHUTDOWN_GRACE_SECONDS once the signal has come.
"""
import asyncio
import logging
import math
import signal
import threading
import time
import weakref
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, TypeVar

import sqlalchemy as sa
from starlette.concurrency import run_in_threadpool

from src.services.assistant.api.route import describe_exception
from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")

SHUTDOWN_GRACE_SECONDS = 8.0


class Stoppable(Protocol):
    def stop(self) -> None:
        """End soon, releasing what is held. Returns at once."""
        ...


class OngoingWork:
    """One per worker. Everything but `drain` runs on the event loop's thread."""

    def __init__(self) -> None:
        # Strong references: the event loop itself only holds weak ones.
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stoppables: set[Stoppable] = set()
        self._draining = asyncio.Event()
        self._drain_started = 0.0

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    def drain(self) -> None:
        """The worker is shutting down: refuse new work and end what can be ended."""
        if self._draining.is_set():
            return
        self._drain_started = time.monotonic()
        self._draining.set()
        logger.info(
            "assistant: draining",
            extra={"to_stop": len(self._stoppables), "tasks": len(self._tasks)},
        )
        for item in list(self._stoppables):
            item.stop()

    def spawn(
        self, work: Coroutine[Any, Any, T], stoppable: Stoppable | None = None
    ) -> asyncio.Task[T]:
        """Run to the end, whatever becomes of the request. `stoppable` is stopped
        when the worker shuts down."""
        task = asyncio.create_task(work)
        self._tasks.add(task)
        if stoppable is not None:
            self._stoppables.add(stoppable)

        def ended(task: asyncio.Task[T]) -> None:
            self._tasks.discard(task)
            if stoppable is not None:
                self._stoppables.discard(stoppable)
            if not task.cancelled():
                # Retrieved here: asyncio would log the text of an exception that
                # nobody waits for any more.
                task.exception()

        task.add_done_callback(ended)
        if stoppable is not None and self.draining:
            stoppable.stop()
        return task

    def grace_left(self) -> float:
        """Seconds a response may stay open; no limit while the worker stays up."""
        if not self.draining:
            return math.inf
        return SHUTDOWN_GRACE_SECONDS - (time.monotonic() - self._drain_started)

    async def wait(self, task: asyncio.Task[Any], *, leave: float) -> None:
        """Until `task` ends, or until the worker shuts down and only `leave` seconds
        of its grace are left. Never cancels `task`."""
        draining = asyncio.create_task(self._draining.wait())
        try:
            await asyncio.wait({task, draining}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                await asyncio.wait({task}, timeout=max(0.0, self.grace_left() - leave))
        finally:
            draining.cancel()

    async def settle(self, timeout: float) -> int:
        """Wait for what is still running, and for what that starts in turn. How many
        tasks did not end in time."""
        deadline = time.monotonic() + timeout
        while self._tasks and (left := deadline - time.monotonic()) > 0:
            await asyncio.wait(set(self._tasks), timeout=left)
        return len(self._tasks)


# Per event loop: as many threads as the pool has connections. Any more would only
# wait for one, on threads that the UI's own sync routes need.
_places: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


async def _take_place() -> asyncio.Semaphore:
    settings = get_settings()
    places = _places.setdefault(
        asyncio.get_running_loop(),
        asyncio.Semaphore(settings.db_pool_size + settings.db_max_overflow),
    )
    try:
        async with asyncio.timeout(settings.db_pool_timeout):
            await places.acquire()
    except TimeoutError:
        # What the pool raises when no connection becomes free.
        raise sa.exc.TimeoutError("all assistant connections are in use") from None
    return places


async def run_db(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """A blocking database function in a thread.

    A thread cannot be interrupted. A cancelled caller gets here only after the
    transaction has ended, so that its cleanup sees what was committed.
    """
    places = await _take_place()
    task = asyncio.ensure_future(run_in_threadpool(function, *args, **kwargs))
    task.add_done_callback(lambda _: places.release())
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        task.exception()  # retrieved
        raise asyncio.CancelledError()
    return task.result()


def install_signal_handlers(
    work: OngoingWork, loop: asyncio.AbstractEventLoop
) -> Callable[[], None]:
    """Drain on SIGTERM and SIGINT, then do what the handler before this one does
    (the gateway's SSE handler, and through it the server's own). Returns what undoes
    it, for a lifespan that ends without the process.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning(
            "assistant: not on the main thread, turns will not end on a signal"
        )
        return lambda: None

    def chained(previous: Any) -> Callable[[int, Any], None]:
        def handler(signum: int, frame: Any) -> None:
            try:
                loop.call_soon_threadsafe(work.drain)
            except RuntimeError:
                pass  # the loop is gone, and with it all work
            if callable(previous):
                previous(signum, frame)

        return handler

    installed: dict[int, tuple[Any, Any]] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(signum)
        handler = chained(previous)
        signal.signal(signum, handler)
        installed[signum] = (handler, previous)

    def uninstall() -> None:
        for signum, (handler, previous) in installed.items():
            if signal.getsignal(signum) is handler:
                signal.signal(signum, previous)

    return uninstall


def log_failure(site: str, message: str, exc: BaseException) -> None:
    """A failure nobody waits for: counted, and logged without its text."""
    metrics.internal_errors_total.labels(site=site).inc()
    logger.error(message, extra=describe_exception(exc))
