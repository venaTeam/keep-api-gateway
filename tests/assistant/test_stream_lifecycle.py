"""However a turn ends, its lock is free and what was said is stored."""
import asyncio
import logging
import signal
import threading
import time

import pytest
import sqlalchemy.exc

from src.services.assistant.api.turn_response import TurnResponse
from src.services.assistant.bl import chat_turn, conversations_bl
from src.services.assistant.bl.chat_turn import Turn
from src.services.assistant.bl.conversations_bl import end_turn, start_turn
from src.services.assistant.core import work as work_module
from src.services.assistant.core.work import (
    OngoingWork,
    install_signal_handlers,
    run_db,
)
from src.services.assistant.gateway.client import GatewayClient
from src.services.assistant.gateway.transport import IN_PROCESS_URL, InProcessTransport
from src.services.assistant.llm.client import TextDelta
from src.services.assistant.models.api.chat import ChatRequest
from src.services.assistant.models.db import Conversation
from tests.assistant.fakes import FakeGateway, alert, identity, query_result, whoami
from tests.assistant.logs import rendered
from tests.assistant.rows import messages, read
from tests.assistant.running import (
    Exchange,
    FakeLlm,
    Gate,
    calls,
    change,
    equip,
    post_chat,
    says,
    until,
)
from tests.fixtures.identity import bearer_headers

CANARY = "canary-30c8f1"


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def gateway():
    return (
        FakeGateway()
        .on_json("GET", "/whoami", whoami())
        .on_json("GET", "/alerts/facets/fields", ["name"])
        .on_json("POST", "/alerts/query", query_result(alert("fp-1")))
    )


def only_conversation(db):
    with db.connect() as conn:
        return conn.execute(Conversation.__table__.select()).mappings().one()


def stored(db) -> list[tuple]:
    return [
        (m["role"], m["content"], m["status"])
        for m in messages(db, only_conversation(db)["id"])
    ]


# --- a client that goes away ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disconnect_interrupts_the_turn(app, db, gateway):
    """Through the whole middleware stack."""
    assert "LoggingMiddleware" in [m.cls.__name__ for m in app.user_middleware]
    gate = Gate()
    llm = FakeLlm([TextDelta("I was about to"), gate, *says(" finish")])
    work = equip(app, llm, gateway)
    exchange = post_chat(app)
    await exchange.until("I was about to")
    assert only_conversation(db)["active_turn_id"] is not None

    exchange.leave()
    started = time.monotonic()
    await exchange.finish()
    assert time.monotonic() - started < 3
    # Before the response is over, not some time later.
    assert only_conversation(db)["active_turn_id"] is None
    assert stored(db)[-1] == ("assistant", "I was about to", "interrupted")
    assert llm.open == 0
    assert await work.settle(0) == 0
    assert "event: done" not in exchange.text


@pytest.mark.asyncio
async def test_a_disconnect_while_the_tool_reads(app, db, gateway, caplog):
    answer = asyncio.Event()

    async def slow(request):
        await answer.wait()
        return FakeGateway().on_json("POST", "/alerts/query", query_result())

    gateway.on("POST", "/alerts/query", slow)
    work = equip(app, FakeLlm(calls(change("fp-1"), text="Let me look.")), gateway)
    exchange = post_chat(app)
    await exchange.until('"phase": "started"')
    exchange.leave()
    await exchange.finish()
    assert only_conversation(db)["active_turn_id"] is None
    assert await work.settle(0) == 0
    # The answer was complete, and stored before the tool ran.
    assert stored(db)[1:] == [("assistant", "Let me look.", "complete")]
    assert not [r for r in caplog.records if "turn not closed" in r.message]
    answer.set()


@pytest.mark.asyncio
async def test_a_client_that_was_never_there(db, gateway):
    """The body iterator is not started at all."""
    ref = await run_db(start_turn, identity(), None, "hello")
    turn = Turn(
        ref,
        identity(),
        ChatRequest(message="hello"),
        gateway.client(),
        FakeLlm([Gate()]),
    )
    turn.start(OngoingWork())

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError(CANARY)

    await TurnResponse(turn)({"type": "http"}, receive, send)
    assert read(db, Conversation, ref.conversation_id)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_a_client_that_left_before_the_response_began(db, gateway):
    """Without any answer the middleware around the stream raises, and the gateway's
    catch-all would log and return that."""
    ref = await run_db(start_turn, identity(), None, "hello")
    turn = Turn(
        ref,
        identity(),
        ChatRequest(message="hello"),
        gateway.client(),
        FakeLlm([Gate()]),
    )
    turn.start(OngoingWork())
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        # As the queue of the middleware: cancelled here when the client is gone.
        await asyncio.sleep(0)
        sent.append(message)

    await TurnResponse(turn)({"type": "http"}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert not sent[1].get("more_body")
    assert read(db, Conversation, ref.conversation_id)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_a_failing_stream_is_logged_without_its_text(db, gateway, caplog):
    work = OngoingWork()
    ref = await run_db(start_turn, identity(), None, "hello")
    turn = Turn(ref, identity(), ChatRequest(message="x"), gateway.client(), FakeLlm())

    async def broken():
        yield "event: x\n\n"
        raise RuntimeError(CANARY)

    turn.frames = broken
    turn.start(work)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        pass

    with caplog.at_level(logging.DEBUG):
        await TurnResponse(turn)({"type": "http"}, receive, send)
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert [r for r in caplog.records if r.message == "assistant: stream failed"]
    assert read(db, Conversation, ref.conversation_id)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_a_request_cancelled_while_the_turn_starts(app, db, gateway, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    real = conversations_bl.start_turn

    def slow(*args):
        ref = real(*args)
        entered.set()
        assert proceed.wait(10)
        return ref

    monkeypatch.setattr(conversations_bl, "start_turn", slow)
    work = equip(app, FakeLlm([Gate()]), gateway)
    exchange = post_chat(app)
    await asyncio.to_thread(entered.wait, 10)
    exchange.task.cancel()
    await asyncio.wait({exchange.task}, timeout=5)
    assert only_conversation(db)["active_turn_id"] is not None
    proceed.set()
    assert await work.settle(5) == 0
    assert only_conversation(db)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_a_turn_that_cannot_be_started_releases_its_lock(
    app, db, gateway, monkeypatch, caplog
):
    def fail(self, work):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(Turn, "start", fail)
    equip(app, FakeLlm(), gateway)
    with caplog.at_level(logging.DEBUG):
        exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert only_conversation(db)["active_turn_id"] is None
    assert not [r for r in caplog.records if CANARY in rendered(r)]


# --- rows and cancellation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_being_stored_is_stored_once(app, db, gateway, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    real = conversations_bl.add_messages

    def slow(*args, **kwargs):
        entered.set()
        assert proceed.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(conversations_bl, "add_messages", slow)
    equip(app, FakeLlm(says("The whole answer.")), gateway)
    exchange = post_chat(app)
    await asyncio.to_thread(entered.wait, 10)
    exchange.leave()
    await asyncio.sleep(0.1)
    proceed.set()
    await exchange.finish()
    assert stored(db)[1:] == [("assistant", "The whole answer.", "complete")]
    assert only_conversation(db)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_a_producer_that_does_not_end_is_left_behind_without_the_lock(
    app, db, gateway, monkeypatch
):
    monkeypatch.setattr(chat_turn, "PRODUCER_END_SECONDS", 0.2)
    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    equip(app, FakeLlm([TextDelta("half"), stubborn, *says(" whole")]), gateway)
    exchange = post_chat(app)
    await exchange.until("half")
    exchange.leave()
    await exchange.finish()
    assert only_conversation(db)["active_turn_id"] is None
    release.set()
    await asyncio.sleep(0.2)
    assert stored(db)[1:] == [("assistant", "half", "interrupted")]


@pytest.mark.asyncio
async def test_a_partial_answer_that_cannot_be_stored_does_not_keep_the_lock(
    app, db, gateway, monkeypatch, caplog
):
    def picky(turn, partial=None):
        if partial is not None:
            raise RuntimeError(CANARY)
        return end_turn(turn)

    monkeypatch.setattr(conversations_bl, "end_turn", picky)
    equip(app, FakeLlm([TextDelta("half"), RuntimeError("x")]), gateway)
    with caplog.at_level(logging.DEBUG):
        exchange = await post_chat(app).finish()
    assert exchange.event("done") == {"message_id": None, "finish_reason": "error"}
    assert only_conversation(db)["active_turn_id"] is None
    assert not [r for r in caplog.records if CANARY in rendered(r)]


@pytest.mark.asyncio
async def test_the_stream_ends_even_when_the_turn_cannot_be_closed(
    app, db, gateway, monkeypatch
):
    def fail(*args):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(conversations_bl, "end_turn", fail)
    equip(app, FakeLlm(), gateway)
    exchange = await post_chat(app).finish()
    assert exchange.event("done")["finish_reason"] == "stop"
    assert CANARY not in exchange.text


@pytest.mark.asyncio
async def test_as_many_threads_as_connections(set_env):
    """The UI's own sync routes need the others."""
    set_env("ASSISTANT_DB_POOL_SIZE", 2)
    set_env("ASSISTANT_DB_MAX_OVERFLOW", 1)
    set_env("ASSISTANT_DB_POOL_TIMEOUT", 0.2)
    proceed, started = threading.Event(), []

    def blocking():
        started.append(1)
        proceed.wait(10)

    running = [asyncio.create_task(run_db(blocking)) for _ in range(3)]
    await until(lambda: len(started) == 3)
    waiting = asyncio.create_task(run_db(blocking))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    with pytest.raises(sqlalchemy.exc.TimeoutError):
        await run_db(blocking)
    assert len(started) == 3
    proceed.set()
    await asyncio.gather(*running)
    # Every place is free again.
    await asyncio.gather(*(run_db(blocking) for _ in range(3)))
    assert len(started) == 6


@pytest.mark.asyncio
async def test_run_db_ends_with_its_thread(monkeypatch):
    proceed, ended = threading.Event(), threading.Event()

    def blocking():
        proceed.wait(10)
        ended.set()

    caller = asyncio.create_task(run_db(blocking))
    await asyncio.sleep(0.05)
    for _ in range(3):
        caller.cancel()
        await asyncio.sleep(0.02)
    assert not caller.done()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert ended.is_set()


# --- heartbeats ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeats_come_while_nothing_else_does(app, db, gateway, set_env):
    set_env("ASSISTANT_HEARTBEAT_SECONDS", 0.1)
    gate = Gate()
    equip(app, FakeLlm([gate, *says("x")]), gateway)
    exchange = post_chat(app)
    await exchange.until(": hb\n\n: hb\n\n")
    gate.opened.set()
    await exchange.finish()
    assert exchange.names == ["conversation", "text_delta", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
async def test_a_slow_alert_query_does_not_hold_up_heartbeats(
    db_session, test_app, db, set_env, monkeypatch
):
    """The gateway's own /alerts/query, called in-process, blocks for two seconds."""
    from src.routes import alerts, assistant

    set_env("ASSISTANT_HEARTBEAT_SECONDS", 0.2)
    test_app.include_router(assistant.router, prefix="/assistant")

    def slow(**kwargs):
        time.sleep(2)
        return []

    monkeypatch.setattr(alerts, "query_last_alerts", slow)
    monkeypatch.setattr(alerts, "enrich_alerts_with_incidents", lambda t, found: found)
    equip(
        test_app,
        FakeLlm(calls(change(cel="true")), says("Nothing matches.")),
        GatewayClient(IN_PROCESS_URL, InProcessTransport(test_app)),
    )
    exchange = Exchange(
        test_app,
        "POST",
        "/assistant/chat",
        {"message": "ack", "context": {}},
        token=bearer_headers("noc")["Authorization"],
    )
    await exchange.finish()
    assert exchange.event("done")["finish_reason"] == "stop"
    assert exchange.event("tool_status")["outcome"] == "no_matches"
    times = [when for when, _ in exchange.chunks]
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert times[-1] - times[0] >= 2
    assert max(gaps) < 1
    assert exchange.text.count(": hb\n\n") >= 5


# --- shutdown --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draining_interrupts_running_turns(app, db, gateway):
    gate = Gate()
    llm = FakeLlm([TextDelta("I was about to"), gate])
    work = equip(app, llm, gateway)
    exchange = post_chat(app)
    await exchange.until("I was about to")
    work.drain()
    await exchange.finish(timeout=3)
    assert exchange.event("done")["finish_reason"] == "cancelled"
    assert exchange.event("done")["message_id"] is not None
    assert "error" not in exchange.names
    assert only_conversation(db)["active_turn_id"] is None
    assert stored(db)[-1] == ("assistant", "I was about to", "interrupted")
    refused = await post_chat(app).finish()
    assert (refused.status, refused.code) == (503, "not_ready")
    # Its message would stay without an answer.
    assert len(stored(db)) == 2


@pytest.mark.asyncio
async def test_a_slow_database_does_not_take_the_workers_last_seconds(
    app, db, gateway, monkeypatch
):
    """gunicorn kills the worker 30 s after the signal, and the gateway's own shutdown
    only begins when the responses have ended."""
    monkeypatch.setattr(work_module, "SHUTDOWN_GRACE_SECONDS", 0.5)
    proceed = threading.Event()

    def stuck(*args):
        proceed.wait(10)
        return end_turn(*args)

    monkeypatch.setattr(conversations_bl, "end_turn", stuck)
    work = equip(app, FakeLlm([TextDelta("I was about to"), Gate()]), gateway)
    exchange = post_chat(app)
    await exchange.until("I was about to")
    started = time.monotonic()
    work.drain()
    try:
        await exchange.finish(timeout=3)
        assert 0.4 < time.monotonic() - started < 2
        assert "done" not in exchange.names
        assert only_conversation(db)["active_turn_id"] is not None
    finally:
        proceed.set()
    # What the worker's last seconds are for.
    assert await work.settle(5) == 0
    assert only_conversation(db)["active_turn_id"] is None
    assert stored(db)[-1] == ("assistant", "I was about to", "interrupted")


@pytest.mark.asyncio
async def test_a_client_that_no_longer_reads(app, db, gateway):
    """The server's send blocks until the connection is lost."""
    stuck, lost = asyncio.Event(), asyncio.Event()

    async def on_send(message):
        if b"half" in message.get("body", b""):
            stuck.set()
            await lost.wait()

    work = equip(app, FakeLlm([TextDelta("half"), Gate()]), gateway)
    exchange = post_chat(app, on_send=on_send)
    await asyncio.wait_for(stuck.wait(), 5)
    exchange.leave()
    await until(lambda: only_conversation(db)["active_turn_id"] is None, 5)
    assert stored(db)[-1] == ("assistant", "half", "interrupted")
    lost.set()
    await exchange.finish()
    assert await work.settle(5) == 0


@pytest.mark.asyncio
async def test_a_chat_that_starts_while_the_worker_begins_to_drain(
    app, db, gateway, monkeypatch
):
    real = conversations_bl.start_turn
    work = equip(app, FakeLlm(), gateway)
    loop = asyncio.get_running_loop()

    def drained_meanwhile(*args):
        ref = real(*args)
        loop.call_soon_threadsafe(work.drain)
        return ref

    monkeypatch.setattr(conversations_bl, "start_turn", drained_meanwhile)
    exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (503, "not_ready")
    assert only_conversation(db)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_work_that_comes_after_the_drain_is_stopped_too(db, gateway):
    work = OngoingWork()
    work.drain()
    ref = await run_db(start_turn, identity(), None, "hello")
    turn = Turn(
        ref, identity(), ChatRequest(message="x"), gateway.client(), FakeLlm([Gate()])
    )
    turn.start(work)
    assert await work.settle(5) == 0
    assert read(db, Conversation, ref.conversation_id)["active_turn_id"] is None


@pytest.mark.asyncio
async def test_settling_is_bounded():
    work = OngoingWork()
    task = work.spawn(asyncio.Event().wait())
    started = time.monotonic()
    assert await work.settle(0.1) == 1
    assert time.monotonic() - started < 1
    task.cancel()


@pytest.mark.asyncio
async def test_a_spawned_task_that_fails_leaves_no_text_behind(caplog):
    async def fail():
        raise RuntimeError(CANARY)

    unretrieved = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: unretrieved.append(context)
    )
    work = OngoingWork()
    with caplog.at_level(logging.DEBUG):
        work.spawn(fail())
        await work.settle(1)
        await asyncio.sleep(0)
    assert not unretrieved
    assert not [r for r in caplog.records if CANARY in rendered(r)]


@pytest.mark.asyncio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_a_signal_drains_and_goes_on_to_the_handler_before(signum):
    seen = []
    original = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        for each in original:
            signal.signal(each, lambda number, frame: seen.append(number))
        before = signal.getsignal(signum)
        work = OngoingWork()
        uninstall = install_signal_handlers(work, asyncio.get_running_loop())
        signal.raise_signal(signum)
        assert seen == [signum]
        await until(lambda: work.draining)
        uninstall()
        assert signal.getsignal(signum) is before
    finally:
        for each, handler in original.items():
            signal.signal(each, handler)


@pytest.mark.asyncio
async def test_undoing_leaves_a_newer_handler_alone():
    original = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    def newer(number, frame):
        pass

    try:
        uninstall = install_signal_handlers(OngoingWork(), asyncio.get_running_loop())
        signal.signal(signal.SIGTERM, newer)
        uninstall()
        assert signal.getsignal(signal.SIGTERM) is newer
        assert signal.getsignal(signal.SIGINT) is original[signal.SIGINT]
    finally:
        for each, handler in original.items():
            signal.signal(each, handler)


def test_off_the_main_thread_no_handler_is_installed(caplog):
    before = signal.getsignal(signal.SIGTERM)
    results = []

    def elsewhere():
        loop = asyncio.new_event_loop()
        results.append(install_signal_handlers(OngoingWork(), loop))
        loop.close()

    thread = threading.Thread(target=elsewhere)
    thread.start()
    thread.join()
    results[0]()
    assert signal.getsignal(signal.SIGTERM) is before
    assert any("not on the main thread" in r.message for r in caplog.records)
    assert work_module.SHUTDOWN_GRACE_SECONDS + 20 < 30
