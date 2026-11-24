"""Work that comes back: when it runs, and that it ends with the worker."""
import asyncio
import logging
import threading
import time

import pytest

from src.services.assistant.core import loops as loops_module
from src.services.assistant.core import metrics
from src.services.assistant.core.loops import Loops
from src.services.assistant.core.work import OngoingWork
from tests.assistant.logs import rendered

CANARY = "canary-3c9f10"


@pytest.fixture(autouse=True)
def waited(monkeypatch):
    """How long the first run was put off; nothing waits for it here."""
    asked = []

    def uniform(low, high):
        asked.append((low, high))
        return 0.0

    monkeypatch.setattr(loops_module.random, "uniform", uniform)
    return asked


async def until(condition, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "it did not happen"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_it_runs_again_and_again(waited):
    work, runs = OngoingWork(), []
    Loops(work).start("some loop", lambda stop: runs.append(stop), seconds=0.01)
    await until(lambda: len(runs) >= 3)
    work.drain()
    assert await work.settle(5) == 0
    assert waited == [(0, 600.0)]


@pytest.mark.asyncio
async def test_not_before_its_turn(monkeypatch):
    monkeypatch.setattr(loops_module.random, "uniform", lambda low, high: high)
    work, runs = OngoingWork(), []
    Loops(work).start("some loop", runs.append, seconds=0.01)
    await asyncio.sleep(0.1)
    assert runs == []
    work.drain()
    assert await work.settle(5) == 0


@pytest.mark.asyncio
async def test_it_waits_between_two_runs():
    work, runs = OngoingWork(), []
    Loops(work).start("some loop", runs.append, seconds=60)
    await until(lambda: runs)
    await asyncio.sleep(0.1)
    assert len(runs) == 1
    work.drain()
    assert await work.settle(5) == 0


@pytest.mark.asyncio
async def test_it_does_not_hold_up_the_worker():
    work, inside = OngoingWork(), threading.Event()
    threads = []

    def run(stop):
        threads.append(threading.current_thread())
        inside.set()
        stop.wait(5)

    Loops(work).start("some loop", run, seconds=60)
    started = time.monotonic()
    await until(inside.is_set)
    await asyncio.sleep(0)
    assert time.monotonic() - started < 2
    assert threads != [threading.current_thread()]
    work.drain()
    assert await work.settle(5) == 0


@pytest.mark.asyncio
async def test_a_failing_run_is_not_the_last(caplog):
    work, runs = OngoingWork(), []
    errors = metrics.internal_errors_total.labels(site="some loop")
    before = errors._value.get()

    def run(stop):
        runs.append(stop)
        if len(runs) == 1:
            raise RuntimeError(CANARY)

    with caplog.at_level(logging.DEBUG):
        Loops(work).start("some loop", run, seconds=0.01)
        await until(lambda: len(runs) >= 2)
    work.drain()
    assert await work.settle(5) == 0
    assert errors._value.get() == before + 1
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert any(getattr(r, "error_type", "") == "RuntimeError" for r in caplog.records)


@pytest.mark.asyncio
async def test_all_of_them_end_with_the_worker():
    """Those that run, and those that wait for their turn."""
    work, inside, ended = OngoingWork(), threading.Event(), threading.Event()
    others = []

    def run(stop):
        inside.set()
        assert stop.wait(5)
        ended.set()

    loops = Loops(work)
    loops.start("running", run, seconds=60)
    loops.start("waiting", others.append, seconds=60)
    await until(lambda: inside.is_set() and others)
    started = time.monotonic()
    work.drain()
    assert await work.settle(5) == 0
    assert time.monotonic() - started < 2
    assert await asyncio.to_thread(ended.wait, 2)


@pytest.mark.asyncio
async def test_none_starts_on_a_worker_that_shuts_down():
    work, runs = OngoingWork(), []
    work.drain()
    Loops(work).start("some loop", runs.append, seconds=0.01)
    assert await work.settle(5) == 0
    assert runs == []
