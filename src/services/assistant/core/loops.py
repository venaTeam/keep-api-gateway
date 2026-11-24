"""Work that comes back, in a thread and never in the way of a shutdown."""
import asyncio
import random
import threading
from collections.abc import Callable

from starlette.concurrency import run_in_threadpool

from src.services.assistant.core.work import OngoingWork, log_failure

# Workers start together.
MAX_JITTER_SECONDS = 600.0

# Blocking. The event is set when it should end soon: a thread cannot be cancelled.
Run = Callable[[threading.Event], None]


class Loops:
    """One per worker."""

    def __init__(self, work: OngoingWork):
        self._work = work
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = threading.Event()

    def start(self, name: str, run: Run, *, seconds: float) -> None:
        """Run after up to ten minutes, and then every `seconds`, until the worker
        shuts down."""
        self._tasks.append(
            self._work.spawn(self._repeat(name, run, seconds), stoppable=self)
        )
        if self._stopping.is_set():
            self.stop()

    def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()

    async def _repeat(self, name: str, run: Run, seconds: float) -> None:
        await asyncio.sleep(random.uniform(0, MAX_JITTER_SECONDS))
        while True:
            try:
                await run_in_threadpool(run, self._stopping)
            except Exception as exc:
                log_failure(name, "assistant: loop failed", exc)
            await asyncio.sleep(seconds)
