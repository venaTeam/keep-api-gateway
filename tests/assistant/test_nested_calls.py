"""Calls of the gateway's own routes inside the process: each one runs in a task of its
own, which no deadline and no disconnect ends."""
import asyncio
import logging
import threading
import time
from collections import Counter

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from src.services.assistant import lifecycle
from src.services.assistant.config import ENABLED_ENV_VAR
from src.services.assistant.gateway.client import (
    MAX_CONCURRENT_ALERT_READS,
    MAX_RUNNING_CALLS,
    Caller,
    GatewayClient,
)
from src.services.assistant.gateway.errors import GatewayUnavailable, QueryFailed
from src.services.assistant.gateway.transport import IN_PROCESS_URL, InProcessTransport
from tests.assistant.fakes import (
    REQUEST_ID,
    TOKEN,
    FakeGateway,
    caller,
    query_result,
    whoami,
)
from tests.assistant.logs import rendered
from tests.fixtures.identity import bearer_headers

CANARY = "canary-7d20c4"


def in_process(app) -> GatewayClient:
    return GatewayClient(IN_PROCESS_URL, InProcessTransport(app))


async def write(client: GatewayClient, timeout: float | None = None):
    return await client.batch_enrich_status(
        caller(), ["a"], "acknowledged", None, False, timeout=timeout
    )


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
    for _ in range(MAX_RUNNING_CALLS):
        with pytest.raises(GatewayUnavailable) as caught:
            await client._send(
                caller(), "query", "POST", "/alerts/query", timeout=0.01, body={}
            )
        assert caught.value.sent is True
    assert slow.started == MAX_RUNNING_CALLS

    with pytest.raises(GatewayUnavailable) as caught:
        await client.query_alerts(caller(), "true", 21)
    assert caught.value.sent is False
    # Nothing was written, so the draft can go back to pending.
    with pytest.raises(GatewayUnavailable) as caught:
        await write(client)
    assert caught.value.sent is False
    assert slow.started == MAX_RUNNING_CALLS

    slow.release.set()
    await until(lambda: slow.finished == MAX_RUNNING_CALLS)
    assert await write(client) == "ok"


@pytest.mark.asyncio
async def test_an_alert_read_keeps_its_place_until_the_call_ends(slow):
    client = slow.fake.client()
    for _ in range(2):  # the second lookup finds every place taken
        with pytest.raises(GatewayUnavailable):
            await client.fetch_alerts_by_fingerprints(
                caller(), [f"q {n}" for n in range(15)], deadline_seconds=0.05
            )
        assert slow.started == MAX_CONCURRENT_ALERT_READS
    slow.release.set()
    await until(lambda: slow.finished == MAX_CONCURRENT_ALERT_READS)
    result = await client.fetch_alerts_by_fingerprints(
        caller(), [f"q {n}" for n in range(15)]
    )
    assert len(result.not_found) == 15


@pytest.mark.asyncio
async def test_a_refused_alert_read_gives_its_place_back(slow):
    client = slow.fake.client()
    blocked = [
        asyncio.create_task(client.query_alerts(caller(), "true", 21))
        for _ in range(MAX_RUNNING_CALLS)
    ]
    await until(lambda: slow.started == MAX_RUNNING_CALLS)
    for _ in range(MAX_CONCURRENT_ALERT_READS + 1):
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
    assert counts["peak"] == MAX_CONCURRENT_ALERT_READS < MAX_RUNNING_CALLS


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
        await client.batch_enrich_status(viewer, ["a"], "acknowledged", None, False)
        == "not_permitted"
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
    client = in_process(test_app)
    noc = Caller(role("noc"), REQUEST_ID)
    queries = [
        asyncio.create_task(client.query_alerts(noc, "true", 21))
        for _ in range(MAX_RUNNING_CALLS)
    ]
    try:
        for _ in queries:
            assert await run_in_threadpool(started.acquire, timeout=10)
        with pytest.raises(GatewayUnavailable) as caught:
            await client.query_alerts(noc, "true", 21)
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
    assert await asyncio.gather(*queries) == [[]] * MAX_RUNNING_CALLS


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
