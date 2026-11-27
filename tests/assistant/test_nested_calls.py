"""Calls of the gateway's own routes inside the process: each one runs in a task of its
own, which no deadline and no disconnect ends."""
import ast
import asyncio
import dataclasses
import itertools
import logging
import threading
import time
from collections import Counter
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

import src.services.assistant
from src.services.assistant import lifecycle
from src.services.assistant.config import ENABLED_ENV_VAR
from src.services.assistant.core import metrics
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.client import (
    CALL_CLASSES,
    MAX_CHAT_CALLS,
    MAX_CRITICAL_ALERT_READS,
    MAX_CRITICAL_CALLS,
    MAX_RUNNING_CALLS,
    CallClass,
    Caller,
    GatewayClient,
)
from src.services.assistant.gateway.errors import GatewayUnavailable, QueryFailed
from src.services.assistant.gateway.transport import IN_PROCESS_URL, InProcessTransport
from tests.assistant.fakes import (
    REQUEST_ID,
    TOKEN,
    FakeGateway,
    alert,
    caller,
    change_status,
    query_result,
    whoami,
)
from tests.assistant.logs import rendered
from tests.fixtures.identity import bearer_headers

CANARY = "canary-7d20c4"
PACKAGE = Path(src.services.assistant.__file__).parent
FILTER_LIMIT = CALL_CLASSES["filter"].limit
ALERT_LIMIT = CALL_CLASSES["alert"].limit
# A chat's call that belongs to no class: the alert-set query of a lookup.
GENERAL = "general"
CHAT = Caller(TOKEN, "00000000-0000-4000-8000-0000000000c1")
CRITICAL = Caller(TOKEN, "00000000-0000-4000-8000-0000000000c2")


def in_process(app) -> GatewayClient:
    return GatewayClient(IN_PROCESS_URL, InProcessTransport(app))


async def write(client: GatewayClient, timeout: float | None = None):
    return await change_status(client, caller(), ["a"], "acknowledged", timeout=timeout)


async def until(condition, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "did not happen"
        await asyncio.sleep(0.01)


class SlowGateway:
    """Every call waits for `release`; `finished` counts the ones that got to the end."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = 0
        self.finished = 0
        self.fake = FakeGateway(self.handle)

    async def handle(self, request):
        self.started += 1
        await self.release.wait()
        self.finished += 1
        if request.url.path == "/whoami":
            return httpx.Response(200, json=whoami())
        if request.url.path == "/alerts/query":
            return httpx.Response(200, json=query_result())
        if request.url.path == "/alerts/batch_enrich":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)


@pytest.fixture()
def slow():
    return SlowGateway()


class Held:
    """Calls to /held wait for `release`; everything else is answered at once."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = 0
        self.fake = FakeGateway(self.handle)

    async def handle(self, request):
        path = request.url.path
        if path == "/held":
            self.started += 1
            await self.release.wait()
            return httpx.Response(200, json={})
        if path == "/whoami":
            return httpx.Response(200, json=whoami())
        if path == "/alerts/query":
            return httpx.Response(200, json=query_result(alert("a")))
        if path == "/alerts/batch_enrich":
            return httpx.Response(200, json={"status": "ok"})
        if path.startswith("/alerts/"):
            return httpx.Response(200, json=alert(path.removeprefix("/alerts/")))
        return httpx.Response(200, json={})


@pytest.fixture()
def held():
    return Held()


def class_call(
    client: GatewayClient,
    name: str,
    *,
    who: Caller = CHAT,
    critical: bool = False,
    deadline_seconds: float | None = None,
    path: str = "/held",
):
    """A read of class `name` (GENERAL: of none)."""
    return client._read(
        who,
        name,
        "GET",
        path,
        call_class=None if name == GENERAL else name,
        critical=critical,
        deadline_seconds=deadline_seconds,
    )


async def take_places(client: GatewayClient, name: str, most: int) -> list:
    """Up to `most` held calls of `name`, each started before the next; stops at the
    first one refused. The ones that got a place."""
    running = []
    for _ in range(most):
        before = len(client._running)
        task = asyncio.create_task(class_call(client, name))
        await until(lambda: task.done() or len(client._running) > before)
        if task.done():
            with pytest.raises(GatewayUnavailable) as caught:
                task.result()
            assert caught.value.sent is False
            break
        running.append(task)
    return running


# --- what a nested route sees --------------------------------------------------


@pytest.mark.asyncio
async def test_a_nested_call_is_the_users_own_request_from_the_users_own_address():
    seen = []
    app = FastAPI()

    @app.get("/whoami")
    async def route(request: Request):
        # Both overlap here: one transport for both would mix the addresses up.
        await asyncio.sleep(0.05)
        seen.append(
            (
                request.client.host,
                request.client.port,
                request.headers["authorization"],
                request.headers["x-request-id"],
            )
        )
        return {}

    client = in_process(app)
    await asyncio.gather(
        client.whoami(Caller("Bearer one", "id-1", ("192.0.2.1", 1111))),
        client.whoami(Caller("Bearer two", "id-2", ("192.0.2.2", 2222))),
    )
    assert sorted(seen) == [
        ("192.0.2.1", 1111, "Bearer one", "id-1"),
        ("192.0.2.2", 2222, "Bearer two", "id-2"),
    ]


@pytest.mark.asyncio
async def test_an_exception_of_a_nested_route_is_its_500(caplog):
    app = FastAPI()

    @app.post("/alerts/query")
    async def query():
        raise RuntimeError(CANARY)

    @app.post("/alerts/batch_enrich")
    async def enrich():
        raise RuntimeError(CANARY)

    client = in_process(app)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(QueryFailed):
            await client.query_alerts(caller(), "true", 21)
        assert await write(client) == "http_error"
    assert not [r for r in caplog.records if CANARY in rendered(r)]


@pytest.mark.asyncio
async def test_a_failure_of_the_transport_itself_is_not_a_crash(caplog):
    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            # What httpx's ASGI transport raises for an app that answers badly.
            raise AssertionError(CANARY)

    client = GatewayClient(IN_PROCESS_URL, Broken())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GatewayUnavailable) as caught:
            await client.whoami(caller())
        assert caught.value.sent is True
        assert await write(client) == "unknown"
    assert not [r for r in caplog.records if CANARY in rendered(r)]


# --- a wait that ends, a call that goes on ---------------------------------------


@pytest.mark.asyncio
async def test_a_deadline_ends_the_wait_and_not_the_write(slow, caplog):
    client = slow.fake.client()
    assert await write(client, timeout=0.05) == "unknown"
    assert (slow.started, slow.finished) == (1, 0)
    slow.release.set()
    assert await client.wait_for_writes(2) == 0
    assert slow.finished == 1
    ended = [r for r in caplog.records if r.message == "gateway: abandoned call ended"]
    assert [(r.endpoint, r.status, r.error_type) for r in ended] == [
        ("batch_enrich", 200, None)
    ]
    assert ended[0].gateway_request_id == REQUEST_ID


@pytest.mark.asyncio
async def test_a_disconnect_ends_the_wait_and_not_the_write(slow):
    client = slow.fake.client()
    waiting = asyncio.create_task(write(client))
    await until(lambda: slow.started == 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    slow.release.set()
    assert await client.wait_for_writes(2) == 0
    assert slow.finished == 1


@pytest.mark.asyncio
async def test_a_lookup_deadline_ends_no_running_call(slow):
    client = slow.fake.client()
    with pytest.raises(GatewayUnavailable):
        await client.fetch_alerts_by_fingerprints(
            caller(), ["a"], deadline_seconds=0.05
        )
    slow.release.set()
    await until(lambda: slow.finished == 1)


@pytest.mark.asyncio
async def test_one_failing_read_ends_no_other_running_call(slow):
    async def handle(request):
        if request.url.path == "/alerts/q q":
            return httpx.Response(502)
        return await slow.handle(request)

    with pytest.raises(GatewayUnavailable):
        await FakeGateway(handle).client().fetch_alerts_by_fingerprints(
            caller(), ["r r", "q q"]
        )
    assert slow.started == 1
    slow.release.set()
    await until(lambda: slow.finished == 1)


@pytest.mark.asyncio
async def test_an_abandoned_call_that_fails_leaves_no_text_behind(slow, caplog):
    async def handle(request):
        await slow.release.wait()
        raise RuntimeError(CANARY)

    unretrieved = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: unretrieved.append(context)
    )
    client = FakeGateway(handle).client()
    with caplog.at_level(logging.DEBUG):
        assert await write(client, timeout=0.02) == "unknown"
        slow.release.set()
        assert await client.wait_for_writes(2) == 0
        await asyncio.sleep(0)
    assert not unretrieved
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert any(getattr(r, "error_type", "") == "RuntimeError" for r in caplog.records)


@pytest.mark.asyncio
async def test_an_answer_held_up_by_a_blocked_loop_is_not_a_timeout():
    async def handle(request):
        time.sleep(0.3)  # what a long batch_enrich does to the event loop
        for _ in range(5):
            await asyncio.sleep(0)
        return httpx.Response(200, json={"status": "ok"})

    assert await write(FakeGateway(handle).client(), timeout=0.1) == "ok"


@pytest.mark.asyncio
async def test_waiting_for_writes_is_bounded_and_counts_no_reads(slow):
    client = slow.fake.client()
    with pytest.raises(GatewayUnavailable):
        await client.fetch_alerts_by_fingerprints(
            caller(), ["a"], deadline_seconds=0.02
        )
    assert await client.wait_for_writes(2) == 0
    assert await write(client, timeout=0.02) == "unknown"
    started = time.monotonic()
    assert await client.wait_for_writes(0.1) == 1
    assert 0.05 < time.monotonic() - started < 1
    slow.release.set()
    assert await client.wait_for_writes(2) == 0


# --- the cap -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_abandoned_calls_count_until_they_end(slow):
    client = slow.fake.client()
    for _ in range(MAX_CHAT_CALLS):
        with pytest.raises(GatewayUnavailable) as caught:
            await client._send(
                caller(), "query", "POST", "/alerts/query", timeout=0.01, body={}
            )
        assert caught.value.sent is True

    with pytest.raises(GatewayUnavailable) as caught:
        await client.query_alerts(caller(), "true", 21)
    assert caught.value.sent is False
    assert slow.started == MAX_CHAT_CALLS

    # The place before the last is for who is asking, and Confirm's and reconcile's
    # looks.
    with pytest.raises(GatewayUnavailable) as caught:
        await client._send(
            caller(), "whoami", "GET", "/whoami", timeout=0.01, critical=True
        )
    assert caught.value.sent is True
    with pytest.raises(GatewayUnavailable) as caught:
        await client.whoami(caller())
    assert caught.value.sent is False
    assert slow.started == MAX_RUNNING_CALLS - 1

    # The last place is for a write.
    assert await write(client, timeout=0.01) == "unknown"
    assert slow.started == MAX_RUNNING_CALLS
    # Nothing was written, so the draft can go back to pending.
    with pytest.raises(GatewayUnavailable) as caught:
        await write(client)
    assert caught.value.sent is False
    assert slow.started == MAX_RUNNING_CALLS

    slow.release.set()
    await until(lambda: slow.finished == MAX_RUNNING_CALLS)
    assert await write(client) == "ok"


@pytest.mark.asyncio
async def test_slow_filters_leave_places_for_everything_else(set_env):
    """A filter the model wrote may run for minutes after its deadline. Nobody else's
    conversation or Confirm may depend on that."""
    set_env("ASSISTANT_GATEWAY_READ_TIMEOUT_SECONDS", 0.05)
    release = asyncio.Event()
    filters = []

    async def handle(request):
        path = request.url.path
        if path in ("/alerts/query", "/alerts/query/count", "/alerts/facets/fields"):
            if b"fingerprint in" not in request.content:
                filters.append(path)
                await release.wait()
            return httpx.Response(200, json=query_result(alert("a")))
        if path == "/whoami":
            return httpx.Response(200, json=whoami())
        return httpx.Response(200, json={"status": "ok"})

    client = FakeGateway(handle).client()
    for _ in range(2):
        assert await client.count_alerts(caller(), "slow") is None
        with pytest.raises(GatewayUnavailable):
            await client.query_alerts(caller(), "slow", 21)
    assert len(filters) == FILTER_LIMIT
    for call in (
        client.query_alerts(caller(), "slow", 21),
        client.alert_fields(caller()),
    ):
        with pytest.raises(GatewayUnavailable) as caught:
            await call
        assert caught.value.sent is False
    assert await client.count_alerts(caller(), "slow") is None
    assert len(filters) == FILTER_LIMIT

    assert (await client.whoami(caller()))["tenant_id"] == "keep"
    assert (await client.fetch_alerts_by_fingerprints(caller(), ["a"])).found
    assert await write(client) == "ok"

    release.set()
    await until(lambda: not client._running)
    assert len(await client.query_alerts(caller(), "fast", 21)) == 1


@pytest.mark.asyncio
async def test_a_filter_call_waits_for_its_place(slow):
    """Several chats at the same moment are not a reason to refuse one."""
    client = slow.fake.client()
    queries = [
        asyncio.create_task(client.query_alerts(caller(), "true", 21))
        for _ in range(FILTER_LIMIT + 2)
    ]
    await until(lambda: slow.started == FILTER_LIMIT)
    slow.release.set()
    assert await asyncio.gather(*queries) == [[]] * (FILTER_LIMIT + 2)


# --- call classes ------------------------------------------------------------------


@pytest.mark.parametrize("name", CALL_CLASSES)
def test_no_class_can_take_every_place(name):
    limit = CALL_CLASSES[name].limit
    assert 0 < limit < MAX_RUNNING_CALLS - 1
    # Nor every place of the chats: one class never shuts out the others.
    assert limit < MAX_CHAT_CALLS


def test_the_places_kept_at_the_top():
    assert (MAX_CHAT_CALLS, MAX_CRITICAL_CALLS) == (
        MAX_RUNNING_CALLS - 2,
        MAX_RUNNING_CALLS - 1,
    )
    assert 0 < MAX_CRITICAL_ALERT_READS < MAX_CRITICAL_CALLS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_CLASSES)
async def test_a_full_class_makes_the_next_call_wait(name, held):
    """Several chats at the same moment are not a reason to refuse one."""
    limit = CALL_CLASSES[name].limit
    assert CALL_CLASSES[name].waits
    client = held.fake.client()
    calls = [asyncio.create_task(class_call(client, name)) for _ in range(limit + 2)]
    await until(lambda: held.started == limit)
    await asyncio.sleep(0.05)
    assert held.started == limit
    assert not any(call.done() for call in calls)
    held.release.set()
    answers = await asyncio.gather(*calls)
    assert [a.status_code for a in answers] == [200] * (limit + 2)
    assert held.started == limit + 2


@pytest.mark.asyncio
async def test_a_class_that_does_not_wait_refuses_at_once(held, monkeypatch):
    monkeypatch.setattr(
        client_module,
        "CALL_CLASSES",
        MappingProxyType({**CALL_CLASSES, "quick": CallClass(limit=1, waits=False)}),
    )
    client = held.fake.client()
    first = asyncio.create_task(class_call(client, "quick"))
    await until(lambda: held.started == 1)
    with pytest.raises(GatewayUnavailable) as caught:
        await class_call(client, "quick", deadline_seconds=5)
    assert caught.value.sent is False
    held.release.set()
    assert (await first).status_code == 200
    assert (await class_call(client, "quick")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_CLASSES)
async def test_the_wait_for_a_place_ends_at_the_callers_deadline(name, held):
    limit = CALL_CLASSES[name].limit
    client = held.fake.client()
    running = await take_places(client, name, limit)
    assert len(running) == limit
    not_sent = metrics.gateway_requests_total.labels(
        endpoint=name, status_class="not_sent"
    )
    before = not_sent._value.get()
    started = time.monotonic()
    with pytest.raises(GatewayUnavailable) as caught:
        await class_call(client, name, deadline_seconds=0.05)
    assert caught.value.sent is False
    assert 0.04 < time.monotonic() - started < 1
    assert not_sent._value.get() == before + 1
    assert held.started == limit
    held.release.set()
    await asyncio.gather(*running)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [*CALL_CLASSES, GENERAL])
async def test_no_read_is_sent_after_the_callers_instant(name, held):
    """A chat turn's last seconds are for the answer."""
    client = held.fake.client()
    late = dataclasses.replace(CHAT, deadline_at=time.monotonic() - 0.01)
    async with asyncio.timeout(2):
        for deadline_seconds in (None, 5):
            with pytest.raises(GatewayUnavailable) as caught:
                await class_call(
                    client, name, who=late, deadline_seconds=deadline_seconds
                )
            assert caught.value.sent is False
    assert held.started == 0
    assert (await class_call(client, name, path="/fast")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_CLASSES)
async def test_the_wait_for_a_place_ends_at_the_callers_instant(name, held):
    limit = CALL_CLASSES[name].limit
    client = held.fake.client()
    running = await take_places(client, name, limit)
    soon = dataclasses.replace(CHAT, deadline_at=time.monotonic() + 0.05)
    started = time.monotonic()
    with pytest.raises(GatewayUnavailable) as caught:
        await class_call(client, name, who=soon, deadline_seconds=5)
    assert caught.value.sent is False
    assert time.monotonic() - started < 1
    assert held.started == limit
    held.release.set()
    await asyncio.gather(*running)


@pytest.mark.asyncio
async def test_the_answer_is_not_awaited_past_the_callers_instant(slow):
    soon = dataclasses.replace(CHAT, deadline_at=time.monotonic() + 0.05)
    started = time.monotonic()
    with pytest.raises(GatewayUnavailable):
        await slow.fake.client().query_alerts(soon, "true", 5, deadline_seconds=5)
    assert time.monotonic() - started < 1
    # The call itself goes on.
    assert slow.started == 1
    slow.release.set()
    await until(lambda: slow.finished == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_CLASSES)
async def test_a_cancelled_wait_for_a_place_takes_none(name, held):
    limit = CALL_CLASSES[name].limit
    client = held.fake.client()
    calls = [asyncio.create_task(class_call(client, name)) for _ in range(limit + 1)]
    await until(lambda: held.started == limit)
    calls.pop().cancel()
    held.release.set()
    await asyncio.gather(*calls)
    await until(lambda: client._class_places[name]._value == limit)
    assert held.started == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_CLASSES)
async def test_a_full_class_leaves_places_for_everything_else(name, held):
    """However slow the calls of one class are, the other chat calls, the critical
    ones and the write still get a place."""
    limit = CALL_CLASSES[name].limit
    client = held.fake.client()
    running = await take_places(client, name, limit)
    others = await take_places(client, GENERAL, MAX_RUNNING_CALLS)
    assert len(others) == MAX_CHAT_CALLS - limit
    # The same class, critical: no class limit applies.
    answer = await class_call(
        client, name, who=CRITICAL, critical=True, path="/fast", deadline_seconds=1
    )
    assert answer.status_code == 200
    assert (await client.whoami(CRITICAL))["tenant_id"] == "keep"
    assert await write(client) == "ok"
    held.release.set()
    await asyncio.gather(*running, *others)


@pytest.mark.asyncio
async def test_a_critical_lookup_reads_alerts_in_places_of_its_own(held):
    """A chat's single-alert reads never hold up a Confirm's look, nor the reverse."""
    client = held.fake.client()
    running = await take_places(client, "alert", ALERT_LIMIT)
    peak = 0

    async def handle(request):
        nonlocal peak
        if request.url.path.startswith("/alerts/q "):
            peak = max(peak, len(client._running) - ALERT_LIMIT)
            await asyncio.sleep(0.01)
        return await held.handle(request)

    held.fake._handler = handle
    result = await client.fetch_alerts_by_fingerprints(
        CRITICAL, [f"q {n}" for n in range(6)], critical=True
    )
    assert sorted(result.found) == [f"q {n}" for n in range(6)]
    assert peak == MAX_CRITICAL_ALERT_READS
    held.release.set()
    await asyncio.gather(*running)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order", list(itertools.permutations([*CALL_CLASSES, GENERAL])), ids=str
)
async def test_no_mix_of_chat_calls_refuses_a_critical_call(order, held):
    """Every class at its limit and general chat calls, in any order: who is asking and
    a Confirm's look still get a place, and so does the write."""
    client = held.fake.client()
    running = []
    for name in order:
        most = MAX_RUNNING_CALLS if name == GENERAL else CALL_CLASSES[name].limit
        running += await take_places(client, name, most)
    assert len(client._running) == len(running) == MAX_CHAT_CALLS

    assert (await client.whoami(CRITICAL))["tenant_id"] == "keep"
    # A Confirm's look: the alert-set query and a single read.
    look = await client.fetch_alerts_by_fingerprints(
        CRITICAL, ["a", "q q"], deadline_seconds=1, critical=True
    )
    assert sorted(look.found) == ["a", "q q"]
    assert await write(client) == "ok"

    # A critical call held up as long as the chats: the write's place stays free.
    stuck = asyncio.create_task(class_call(client, GENERAL, who=CRITICAL, critical=True))
    await until(lambda: len(client._running) == MAX_CRITICAL_CALLS)
    assert await write(client) == "ok"
    # Two critical calls at once, at full saturation: the second is refused.
    with pytest.raises(GatewayUnavailable) as caught:
        await client.whoami(CRITICAL)
    assert caught.value.sent is False
    held.release.set()
    await asyncio.gather(*running, stuck)


def test_only_who_is_asking_confirm_and_reconcile_take_critical_places():
    """Critical places are for the identity check (inside the client), Confirm's looks
    and reconcile's: no tool, kind or turn asks for them."""
    allowed = {
        "gateway/client.py",
        "gateway/reader.py",
        "bl/confirm_bl.py",
        "bl/reconcile_bl.py",
    }
    asking = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(
                keyword.arg == "critical" for keyword in node.keywords
            ):
                asking.add(path.relative_to(PACKAGE).as_posix())
    assert asking <= allowed
    assert {"bl/confirm_bl.py", "bl/reconcile_bl.py"} <= asking


@pytest.mark.asyncio
async def test_an_alert_read_keeps_its_place_until_the_call_ends(slow):
    client = slow.fake.client()
    for _ in range(2):  # the second lookup finds every place taken
        with pytest.raises(GatewayUnavailable):
            await client.fetch_alerts_by_fingerprints(
                caller(), [f"q {n}" for n in range(15)], deadline_seconds=0.05
            )
        assert slow.started == ALERT_LIMIT
    slow.release.set()
    await until(lambda: slow.finished == ALERT_LIMIT)
    result = await client.fetch_alerts_by_fingerprints(
        caller(), [f"q {n}" for n in range(15)]
    )
    assert len(result.not_found) == 15


@pytest.mark.asyncio
async def test_a_refused_alert_read_gives_its_place_back(slow):
    client = slow.fake.client()
    blocked = [
        asyncio.create_task(client.whoami(caller()))
        for _ in range(MAX_RUNNING_CALLS - 1)
    ]
    await until(lambda: slow.started == MAX_RUNNING_CALLS - 1)
    for _ in range(ALERT_LIMIT + 1):
        with pytest.raises(GatewayUnavailable) as caught:
            await client.fetch_alerts_by_fingerprints(caller(), ["q q"])
        assert caught.value.sent is False
    slow.release.set()
    await asyncio.gather(*blocked)
    result = await client.fetch_alerts_by_fingerprints(caller(), ["q q"])
    assert result.not_found == ["q q"]


@pytest.mark.asyncio
async def test_one_chats_own_lookup_fits_under_the_cap():
    counts = Counter()

    async def handle(request):
        counts["running"] += 1
        counts["peak"] = max(counts["peak"], counts["running"])
        await asyncio.sleep(0.01)
        counts["running"] -= 1
        return httpx.Response(404)

    client = FakeGateway(handle).client()
    result = await client.fetch_alerts_by_fingerprints(
        caller(), [f"q {n}" for n in range(20)]
    )
    assert len(result.not_found) == 20
    assert counts["peak"] == ALERT_LIMIT < MAX_CHAT_CALLS


# --- through the real app --------------------------------------------------------


def role(name: str) -> str:
    return bearer_headers(name)["Authorization"]


@pytest.mark.asyncio
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
async def test_the_gateways_own_verifier_and_scopes_decide(db_session, test_app):
    client = in_process(test_app)
    noc = Caller(role("noc"), REQUEST_ID)
    body = await client.whoami(noc)
    assert (body["email"], "write:alert" in body["scopes"]) == ("noc@example.com", True)
    assert await client.query_alerts(noc, "severity == 'critical'", 21) == []
    viewer = Caller(role("viewer"), REQUEST_ID)
    assert (
        await change_status(client, viewer, ["a"], "acknowledged") == "not_permitted"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
async def test_a_full_house_of_calls_leaves_the_ui_its_threads(
    db_session, test_app, monkeypatch
):
    from src.routes import alerts

    release = threading.Event()
    started = threading.Semaphore(0)

    def stuck(**kwargs):
        started.release()
        assert release.wait(20)
        return []

    monkeypatch.setattr(alerts, "query_last_alerts", stuck)
    # The test database is one SQLite connection: not for several threads at once.
    monkeypatch.setattr(alerts, "enrich_alerts_with_incidents", lambda t, found: found)
    client = in_process(test_app)
    noc = Caller(role("noc"), REQUEST_ID)
    # What a lookup of fingerprints sends, which the limit for filters does not count:
    # every place of the chats, and the critical one.
    queries = [
        asyncio.create_task(client._query(noc, "true", 21))
        for _ in range(MAX_CHAT_CALLS)
    ] + [asyncio.create_task(client._query(noc, "true", 21, critical=True))]
    assert len(queries) == MAX_RUNNING_CALLS - 1
    try:
        for _ in queries:
            assert await run_in_threadpool(started.acquire, timeout=10)
        for critical in (False, True):
            with pytest.raises(GatewayUnavailable) as caught:
                await client._query(noc, "true", 21, critical=critical)
            assert caught.value.sent is False
        assert not started.acquire(blocking=False)

        ui = httpx.AsyncClient(
            transport=httpx.ASGITransport(test_app), base_url="http://ui.test"
        )
        # A sync route: it needs a thread of the same pool.
        response = await asyncio.wait_for(
            ui.get("/whoami", headers={"Authorization": role("viewer")}), 5
        )
        assert response.status_code == 200
    finally:
        release.set()
    assert await asyncio.gather(*queries) == [[]] * (MAX_RUNNING_CALLS - 1)


def test_the_worker_calls_its_own_app(make_app, set_env, monkeypatch):
    set_env(ENABLED_ENV_VAR, "true")
    app = make_app()

    @app.get("/nested/{name}")
    async def nested(name: str, request: Request):
        return {"name": name, "authorization": request.headers["authorization"]}

    lifecycle.start(app)
    gateway = app.state.assistant_gateway

    async def call():
        response = await gateway._send(caller(), "query", "GET", "/nested/x", timeout=5)
        await lifecycle.stop(app)
        return response.json()

    assert asyncio.run(call()) == {"name": "x", "authorization": TOKEN}
    assert gateway._http.is_closed
