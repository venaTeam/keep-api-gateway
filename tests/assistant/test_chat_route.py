"""POST /assistant/chat through the real app: what a turn streams and stores."""
import asyncio
import json
import logging
import uuid

import pytest
import sqlalchemy as sa

from src.services.assistant.bl import conversations_bl
from src.services.assistant.core import metrics
from src.services.assistant.llm.client import (
    Finished,
    LlmError,
    TextDelta,
    make_safety_identifier,
)
from src.services.assistant.models.api.stream import STREAM_HEADERS
from src.services.assistant.models.db import Conversation, Draft
from tests.assistant.fakes import FakeGateway, alert, identity, query_result, whoami
from tests.assistant.logs import rendered
from tests.assistant.rows import add_conversation, messages, read
from tests.assistant.running import (
    FakeLlm,
    Gate,
    calls,
    change,
    equip,
    post_chat,
    says,
)

QUERY = ("POST", "/alerts/query")
CANARY = "canary-e5a1b9"


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
        .on_json("GET", "/alerts/facets/fields", ["name", "labels.team"])
        .on_json(*QUERY, query_result(alert("fp-1"), alert("fp-2")))
    )


def ids(exchange) -> tuple[uuid.UUID, dict]:
    opening = exchange.event("conversation")
    return uuid.UUID(opening["conversation_id"]), opening


def lock(db, conversation_id):
    return read(db, Conversation, conversation_id)["active_turn_id"]


def counter(metric, **labels) -> float:
    return (metric.labels(**labels) if labels else metric)._value.get()


# --- an answer -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_answer(app, db, gateway):
    llm = FakeLlm(says("Hel", "lo."))
    equip(app, llm, gateway)
    before = counter(metrics.turns_total, result="ok")
    exchange = await post_chat(app, "hi there", page="/alerts/feed").finish()

    assert exchange.status == 200
    assert exchange.headers["content-type"] == "text/event-stream"
    for name, value in STREAM_HEADERS.items():
        assert exchange.headers[name.lower()] == value
    assert exchange.names == ["conversation", "text_delta", "text_delta", "done"]
    conversation, opening = ids(exchange)
    message = exchange.event("text_delta")["message_id"]
    assert exchange.event("done") == {"message_id": message, "finish_reason": "stop"}

    user, answer = messages(db, conversation)
    assert (user["role"], user["content"]) == ("user", "hi there")
    assert str(user["id"]) == opening["user_message_id"]
    assert (answer["role"], answer["content"]) == ("assistant", "Hello.")
    assert (str(answer["id"]), answer["status"]) == (message, "complete")
    assert (answer["model"], answer["prompt_tokens"]) == ("fake-model", 100)
    assert answer["completion_tokens"] == 10
    assert lock(db, conversation) is None
    assert counter(metrics.turns_total, result="ok") == before + 1


@pytest.mark.asyncio
async def test_what_the_model_is_sent(app, db, gateway):
    llm = FakeLlm()
    equip(app, llm, gateway)
    await post_chat(app, "ack these", selected_fingerprints=["fp-1"]).finish()
    [request] = llm.requests
    system, context, user = request.messages
    assert system["role"] == "system"
    assert "fp-1" not in system["content"]
    assert context["role"] == "user"
    assert context["content"].startswith("<untrusted_data>\n")
    assert '"fingerprint":"fp-1"' in context["content"]
    assert "labels.team" in context["content"]
    assert user == {"role": "user", "content": "ack these"}
    assert [tool.name for tool in request.tools] == ["change_alert_status"]
    assert request.tool_choice == "auto"
    me = identity()
    assert request.safety_identifier == make_safety_identifier(me.tenant_id, me.email)
    # None of these ever leave the gateway.
    sent = json.dumps(request.messages)
    for secret in (me.email, me.authorization):
        assert secret not in sent


@pytest.mark.asyncio
async def test_the_next_message_knows_the_first_and_not_its_context(app, db, gateway):
    llm = FakeLlm(says("One."), says("Two."))
    equip(app, llm, gateway)
    first = await post_chat(app, "first", selected_fingerprints=["fp-1"]).finish()
    conversation, _ = ids(first)
    second = await post_chat(app, "second", conversation).finish()
    assert ids(second)[0] == conversation
    _, one, answer, context, two = llm.requests[1].messages
    assert one == {"role": "user", "content": "first"}
    assert answer == {"role": "assistant", "content": "One."}
    assert "fp-1" not in context["content"]
    assert two == {"role": "user", "content": "second"}
    assert len(messages(db, conversation)) == 4


@pytest.mark.asyncio
async def test_tokens_are_counted_per_tenant_and_model(app, db, gateway):
    equip(app, FakeLlm(), gateway)
    labels = {"tenant_id": "keep", "model": "fake-model"}
    before = counter(metrics.llm_tokens_total, type="prompt", **labels)
    await post_chat(app).finish()
    assert counter(metrics.llm_tokens_total, type="prompt", **labels) == before + 100


# --- drafts ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft(app, db, gateway):
    llm = FakeLlm(
        calls(change("fp-1", "fp-2"), text="Looking."), says("Confirm it on the card.")
    )
    equip(app, llm, gateway)
    created = counter(metrics.drafts_total, kind="change_alert_status", event="created")
    exchange = await post_chat(app, "ack them").finish()
    assert exchange.names == [
        "conversation",
        "text_delta",
        "tool_status",
        "draft",
        "tool_status",
        "text_delta",
        "done",
    ]
    conversation, _ = ids(exchange)
    draft = exchange.event("draft")["draft"]
    assert (draft["state"], draft["conversation_id"]) == ("pending", str(conversation))
    assert [a["fingerprint"] for a in draft["payload"]["alerts"]] == ["fp-1", "fp-2"]
    started, finished = [d for n, d in exchange.events if n == "tool_status"]
    assert (started["phase"], finished["phase"]) == ("started", "finished")
    assert finished["outcome"] == "draft_created"
    assert started["label"] == "Preparing a status change"
    texts = [d["message_id"] for n, d in exchange.events if n == "text_delta"]
    assert len(set(texts)) == 2
    assert exchange.event("done")["message_id"] == texts[1]

    rows = messages(db, conversation)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[1]["tool_calls"][0]["id"] == rows[2]["tool_call_id"] == "call_1"
    assert str(rows[2]["draft_id"]) == draft["id"]
    assert read(db, Draft, draft["id"])["created_by"] == identity().email
    assert (
        counter(metrics.drafts_total, kind="change_alert_status", event="created")
        == created + 1
    )

    _, _, _, assistant, reply = llm.requests[1].messages
    assert assistant["tool_calls"][0]["id"] == reply["tool_call_id"] == "call_1"
    assert '"status":"draft_created"' in reply["content"]
    assert reply["content"].startswith("<untrusted_data>\n")


@pytest.mark.asyncio
async def test_a_draft_is_committed_before_its_event(app, db, gateway):
    seen = []

    async def on_send(message):
        body = message.get("body", b"").decode()
        if body.startswith("event: draft\n"):
            draft = json.loads(body.split("data: ", 1)[1])["draft"]
            row = await asyncio.to_thread(read, db, Draft, draft["id"])
            seen.append(row and row["state"])

    equip(app, FakeLlm(calls(change("fp-1")), says("ok")), gateway)
    await post_chat(app, "ack", on_send=on_send).finish()
    assert seen == ["pending"]


@pytest.mark.asyncio
async def test_the_lock_is_free_when_done_arrives(app, db, gateway):
    seen = []

    async def on_send(message):
        body = message.get("body", b"").decode()
        if body.startswith("event: done\n"):
            with db.connect() as conn:
                seen.append(conn.scalar(sa.select(Conversation.active_turn_id)))

    equip(app, FakeLlm(), gateway)
    await post_chat(app, on_send=on_send).finish()
    assert seen == [None]


@pytest.mark.asyncio
async def test_at_most_eight_tool_rounds(app, db, gateway):
    llm = FakeLlm(
        *(calls(change(f"gone-{n}"), first=n) for n in range(8)), says("I give up.")
    )
    gateway.on_json(*QUERY, query_result())
    equip(app, llm, gateway)
    before = counter(metrics.turns_total, result="tool_limit")
    exchange = await post_chat(app, "ack").finish()
    assert [r.tool_choice for r in llm.requests] == ["auto"] * 8 + ["none"]
    assert exchange.event("done")["finish_reason"] == "tool_limit"
    assert exchange.event("text_delta")["delta"] == "I give up."
    assert counter(metrics.turns_total, result="tool_limit") == before + 1


@pytest.mark.asyncio
async def test_calls_after_the_last_round_are_ignored(app, db, gateway):
    equip(app, FakeLlm(calls(change("fp-1"), text="x")), gateway)
    exchange = await post_chat(app, "ack").finish()
    assert exchange.names.count("draft") == 3
    assert exchange.event("done")["finish_reason"] == "stop"
    rows = messages(db, ids(exchange)[0])
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["tool_calls"] is None


@pytest.mark.asyncio
async def test_at_most_three_drafts(app, db, gateway):
    # The fourth call comes in one round with the third.
    llm = FakeLlm(
        calls(change("fp-1"), first=1),
        calls(change("fp-1"), first=2),
        calls(change("fp-1"), change("fp-2"), first=3),
        says("That is all."),
    )
    equip(app, llm, gateway)
    exchange = await post_chat(app, "ack").finish()
    assert exchange.names.count("draft") == 3
    assert [r.tool_choice for r in llm.requests] == ["auto"] * 3 + ["none"]
    assert '"code":"draft_limit"' in llm.requests[-1].messages[-1]["content"]
    assert exchange.event("tool_status")["outcome"] == "error"
    assert exchange.event("done")["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_an_unknown_tool_is_an_error_for_the_model_only(app, db, gateway):
    llm = FakeLlm(calls({}, name="delete_everything"), says("I cannot do that."))
    equip(app, llm, gateway)
    before = counter(metrics.tool_calls_total, tool="unknown", outcome="error")
    exchange = await post_chat(app, "x").finish()
    assert "tool_status" not in exchange.names
    assert '"code":"unknown_tool"' in llm.requests[1].messages[-1]["content"]
    assert (
        counter(metrics.tool_calls_total, tool="unknown", outcome="error") == before + 1
    )


@pytest.mark.asyncio
async def test_unusable_arguments_go_back_to_the_model(app, db, gateway):
    llm = FakeLlm(calls('{"fingerprints": ["fp-'), says("Again?"))
    equip(app, llm, gateway)
    exchange = await post_chat(app, "ack").finish()
    assert exchange.event("tool_status")["outcome"] == "invalid_arguments"
    sent = llm.requests[1].messages[-2]["tool_calls"][0]["function"]
    assert sent["arguments"] == "{}"


@pytest.mark.asyncio
async def test_cel_lookups_are_counted_over_the_whole_turn(app, db, gateway):
    gateway.on_json(*QUERY, query_result())
    by_cel = change(cel="true")
    llm = FakeLlm(calls(*[by_cel] * 7), says("ok"))
    equip(app, llm, gateway)
    await post_chat(app, "ack").finish()
    assert gateway.paths().count("POST /alerts/query") == 6
    assert '"code":"cel_lookup_limit"' in llm.requests[1].messages[-1]["content"]


# --- failures in the stream ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    [
        (401, "unauthenticated", "unauthenticated"),
        (502, "error", "gateway_unavailable"),
    ],
)
async def test_a_tool_that_ends_the_turn(app, db, gateway, status, outcome, code):
    gateway.on_json(*QUERY, {}, status=status)
    equip(app, FakeLlm(calls(change(cel="true"))), gateway)
    exchange = await post_chat(app, "ack").finish()
    assert exchange.names == [
        "conversation",
        "tool_status",
        "tool_status",
        "error",
        "done",
    ]
    assert exchange.event("tool_status")["outcome"] == outcome
    assert exchange.event("error")["code"] == code
    assert exchange.event("done") == {"message_id": None, "finish_reason": "error"}
    conversation, _ = ids(exchange)
    assert lock(db, conversation) is None
    assert [m["role"] for m in messages(db, conversation)] == [
        "user",
        "assistant",
        "tool",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("unavailable", "llm_unavailable"),
        ("timeout", "llm_unavailable"),
        ("rate_limited", "llm_rate_limited"),
        ("rejected", "llm_rejected"),
    ],
)
async def test_an_llm_failure_keeps_the_text_so_far(app, db, gateway, kind, code):
    equip(app, FakeLlm([TextDelta("I was ab"), LlmError(kind)]), gateway)
    before = counter(metrics.turns_total, result="error")
    exchange = await post_chat(app).finish()
    assert exchange.names == ["conversation", "text_delta", "error", "done"]
    assert exchange.event("error")["code"] == code
    conversation, _ = ids(exchange)
    partial = messages(db, conversation)[-1]
    assert (partial["content"], partial["status"]) == ("I was ab", "error")
    assert exchange.event("done")["message_id"] == str(partial["id"])
    assert lock(db, conversation) is None
    assert counter(metrics.turns_total, result="error") == before + 1


@pytest.mark.asyncio
async def test_a_turn_has_a_deadline(app, db, gateway, set_env):
    set_env("ASSISTANT_TURN_DEADLINE_SECONDS", 0.3)
    gate = Gate()
    llm = FakeLlm([TextDelta("Let me"), gate])
    equip(app, llm, gateway)
    before = counter(metrics.turns_total, result="timeout")
    exchange = await post_chat(app).finish(timeout=5)
    assert exchange.event("error")["code"] == "turn_timeout"
    assert exchange.event("done")["finish_reason"] == "error"
    conversation, _ = ids(exchange)
    assert messages(db, conversation)[-1]["status"] == "error"
    assert lock(db, conversation) is None
    assert llm.open == 0
    assert counter(metrics.turns_total, result="timeout") == before + 1


@pytest.mark.asyncio
async def test_a_bug_leaves_no_text_in_the_stream_or_the_logs(
    app, db, gateway, caplog, monkeypatch
):
    equip(app, FakeLlm([TextDelta("x"), RuntimeError(CANARY)]), gateway)
    with caplog.at_level(logging.DEBUG):
        exchange = await post_chat(app).finish()
    assert exchange.event("error")["code"] == "internal"
    assert CANARY not in exchange.text
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert any(getattr(r, "error_type", "") == "RuntimeError" for r in caplog.records)
    assert lock(db, ids(exchange)[0]) is None


@pytest.mark.asyncio
async def test_text_that_postgres_cannot_hold(app, db, gateway):
    bad = "a\x00b\ud800c"
    llm = FakeLlm([*calls(bad, text=bad)], says("ok"))
    equip(app, llm, gateway)
    exchange = await post_chat(app).finish()
    assert exchange.event("done")["finish_reason"] == "stop"
    rows = messages(db, ids(exchange)[0])
    assert rows[1]["content"] == rows[1]["tool_calls"][0]["arguments"] == "ab?c"
    json.dumps(llm.requests[1].messages, ensure_ascii=False).encode()


@pytest.mark.asyncio
async def test_an_empty_answer_stores_nothing(app, db, gateway):
    equip(app, FakeLlm([Finished("length")]), gateway)
    exchange = await post_chat(app).finish()
    assert exchange.event("done") == {"message_id": None, "finish_reason": "length"}
    assert [m["role"] for m in messages(db, ids(exchange)[0])] == ["user"]


@pytest.mark.asyncio
async def test_a_turn_that_lost_its_conversation_ends(app, db, gateway):
    gate = Gate()
    equip(app, FakeLlm([gate, *says("late")]), gateway)
    exchange = post_chat(app)
    await gate.reached.wait()
    with db.begin() as conn:
        conn.execute(sa.update(Conversation).values(active_turn_id=uuid.uuid4()))
    gate.opened.set()
    await exchange.finish()
    assert exchange.event("error")["code"] == "internal"
    assert len(messages(db, ids(exchange)[0])) == 1


# --- before the stream -----------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_key_only_chat_is_refused(app, db, gateway):
    equip(app, None, gateway)
    exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (503, "llm_not_configured")
    with db.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(Conversation)) == 0


@pytest.mark.asyncio
async def test_a_draining_worker_takes_no_chat(app, db, gateway):
    equip(app, FakeLlm(), gateway).drain()
    exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (503, "not_ready")


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_id", [str(uuid.uuid4()), "not-a-uuid"])
async def test_an_unknown_conversation(app, db, gateway, conversation_id):
    equip(app, FakeLlm(), gateway)
    exchange = await post_chat(app, conversation_id=conversation_id).finish()
    assert (exchange.status, exchange.code) == (404, "conversation_not_found")


@pytest.mark.asyncio
async def test_a_second_message_during_a_turn(app, db, gateway):
    gate = Gate()
    equip(app, FakeLlm([gate, *says("x")]), gateway)
    first = post_chat(app)
    await first.until("event: conversation")
    conversation, _ = ids(first)
    second = await post_chat(app, "again", conversation).finish()
    assert (second.status, second.code) == (409, "turn_in_progress")
    gate.opened.set()
    await first.finish()
    third = await post_chat(app, "again", conversation).finish()
    assert third.status == 200


@pytest.mark.asyncio
async def test_the_limits_answer_429_with_retry_after(app, db, gateway, set_env):
    set_env("ASSISTANT_RATE_LIMIT_PER_MINUTE", 1)
    equip(app, FakeLlm(), gateway)
    await post_chat(app).finish()
    exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (429, "rate_limited")
    assert 1 <= int(exchange.headers["retry-after"]) <= 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"), [(401, "unauthenticated"), (403, "forbidden")]
)
async def test_whoami_decides_first(app, db, gateway, status, code):
    gateway.on_json("GET", "/whoami", {}, status=status)
    equip(app, FakeLlm(), gateway)
    exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (status, code)


@pytest.mark.asyncio
async def test_a_bad_body_is_400(app, db, gateway):
    equip(app, FakeLlm(), gateway)
    exchange = await post_chat(app, message="   ").finish()
    assert (exchange.status, exchange.code) == (400, "invalid_value")


@pytest.mark.asyncio
async def test_a_failing_database_is_a_fixed_500(app, db, gateway, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise sa.exc.OperationalError(CANARY, {}, Exception(CANARY))

    monkeypatch.setattr(conversations_bl, "start_turn", fail)
    equip(app, FakeLlm(), gateway)
    with caplog.at_level(logging.DEBUG):
        exchange = await post_chat(app).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert CANARY not in exchange.text
    assert not [r for r in caplog.records if CANARY in rendered(r)]


@pytest.mark.asyncio
async def test_other_users_cannot_write_into_a_conversation(app, db, gateway):
    conversation = add_conversation(db, created_by="else@example.com")
    equip(app, FakeLlm(), gateway)
    exchange = await post_chat(app, "x", conversation).finish()
    assert (exchange.status, exchange.code) == (404, "conversation_not_found")
    assert messages(db, conversation) == []


# --- what is logged ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_line_per_turn_with_ids_and_counts_only(app, db, gateway, caplog):
    equip(app, FakeLlm(calls(change("fp-1")), says("ok")), gateway)
    with caplog.at_level(logging.DEBUG):
        exchange = await post_chat(app, CANARY).finish()
    [line] = [r for r in caplog.records if r.message == "assistant: turn finished"]
    assert line.turn_id == exchange.event("conversation")["turn_id"]
    assert (line.result, line.rounds, line.drafts) == ("ok", 1, 1)
    assert "ops@example.com" not in rendered(line)
    assert not [r for r in caplog.records if CANARY in rendered(r)]
