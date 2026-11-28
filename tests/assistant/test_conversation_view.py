"""GET /assistant/conversations/{id}: what a reload shows."""
import uuid

import pytest
import sqlalchemy as sa

from src.services.assistant.bl.timeline import LINK_RESULT_PREFIX
from src.services.assistant.core.db import get_engine
from src.services.assistant.llm.client import TextDelta
from src.services.assistant.models.api.conversation import TIMELINE_LIMIT
from src.services.assistant.models.db import Conversation
from src.services.assistant.tools.base import encode_result
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.fakes import FakeGateway, alert, query_result, whoami
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    add_message,
    ago,
    draft_payload,
    read,
)
from tests.assistant.running import (
    Exchange,
    FakeLlm,
    Gate,
    calls,
    change,
    equip,
    post_chat,
    says,
)


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture(autouse=True)
def worker(app):
    return equip(app)


def get(app, conversation_id) -> Exchange:
    return Exchange(app, "GET", f"/assistant/conversations/{conversation_id}")


async def view(app, conversation_id) -> dict:
    exchange = await get(app, conversation_id).finish()
    assert exchange.status == 200, exchange.text
    return exchange.json()


def texts(body: dict) -> list:
    shown = {"message": "content", "link": "path"}
    return [
        item[shown[item["type"]]] if item["type"] in shown else item["draft"]["id"]
        for item in body["items"]
    ]


@pytest.mark.asyncio
async def test_a_whole_conversation(app, db):
    """As a chat leaves it behind."""
    gateway = (
        FakeGateway()
        .on_json("GET", "/whoami", whoami())
        .on_json("GET", "/alerts/facets/fields", ["name"])
        .on_json("POST", "/alerts/query", query_result(alert("fp-1")))
    )
    equip(
        app,
        FakeLlm(
            calls(change("fp-1"), text="I will draft it."), says("Please confirm.")
        ),
        gateway,
    )
    chat = await post_chat(app, "ack fp-1").finish()
    conversation = chat.event("conversation")["conversation_id"]

    body = await view(app, conversation)
    assert set(body) == {
        "id",
        "title",
        "created_at",
        "updated_at",
        "turn_in_progress",
        "has_more",
        "items",
    }
    assert body["id"] == conversation
    assert body["title"] == "ack fp-1"
    assert (body["turn_in_progress"], body["has_more"]) == (False, False)
    user, first, draft, last = body["items"]
    assert user == {
        "type": "message",
        "id": chat.event("conversation")["user_message_id"],
        "role": "user",
        "content": "ack fp-1",
        "status": "complete",
        "created_at": user["created_at"],
    }
    assert (first["role"], first["content"]) == ("assistant", "I will draft it.")
    # The same as in the stream.
    assert draft == {"type": "draft", "draft": chat.event("draft")["draft"]}
    assert (last["content"], last["id"]) == (
        "Please confirm.",
        chat.event("done")["message_id"],
    )


@pytest.mark.asyncio
async def test_only_what_can_be_seen(app, db):
    conversation = add_conversation(db)
    draft = add_draft(db, conversation)
    add_message(db, conversation, "user", content="do it")
    # Only tool calls, no text.
    add_message(db, conversation, "assistant", content="", tool_calls=[{"id": "1"}])
    add_message(db, conversation, "assistant", content=None, tool_calls=[{"id": "2"}])
    add_message(db, conversation, "tool", content="{}", tool_call_id="1")
    add_message(
        db, conversation, "tool", content="{}", tool_call_id="2", draft_id=draft
    )
    add_message(db, conversation, "event", content="applied", draft_id=draft)
    add_message(db, conversation, "assistant", content="cut", status="interrupted")
    body = await view(app, conversation)
    assert texts(body) == ["do it", str(draft), "cut"]
    assert body["items"][2]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_the_draft_is_shown_as_it_is_now(app, db):
    conversation = add_conversation(db)
    draft = add_draft(
        db,
        conversation,
        state="discarded",
        resolved_at=ago(minutes=1),
        permitted=False,
        payload=draft_payload("fp-9", target="resolved", note="a note"),
    )
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    [item] = (await view(app, conversation))["items"]
    assert item["draft"]["state"] == "discarded"
    assert item["draft"]["permitted"] is False
    assert item["draft"]["resolved_at"] is not None
    assert item["draft"]["payload"]["note"] == "a note"
    assert item["draft"]["payload"]["alerts"][0]["fingerprint"] == "fp-9"
    assert item["draft"]["outcome"] is None


@pytest.mark.asyncio
async def test_the_order_is_the_order_of_the_inserts(app, db):
    """Rows of one transaction share their created_at."""
    conversation = add_conversation(db)
    same = ago(minutes=1)
    for n in range(5):
        add_message(db, conversation, content=f"m{n}", created_at=same)
    add_message(db, conversation, content="clock went back", created_at=ago(days=1))
    assert texts(await view(app, conversation)) == [
        *(f"m{n}" for n in range(5)),
        "clock went back",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_the_last_hundred_items(app, db, extra):
    assert TIMELINE_LIMIT == 100
    conversation = add_conversation(db)
    count = TIMELINE_LIMIT + extra
    for n in range(count):
        add_message(db, conversation, content=f"m{n}")
        # What is not shown does not use up the limit.
        add_message(db, conversation, "tool", tool_call_id="1")
    body = await view(app, conversation)
    assert texts(body) == [f"m{n}" for n in range(extra, count)]
    assert body["has_more"] is bool(extra)


@pytest.mark.asyncio
async def test_drafts_are_read_in_one_statement(app, db):
    conversation = add_conversation(db)
    for n in range(10):
        draft = add_draft(db, conversation)
        add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    statements = []
    engine = get_engine()

    def count(conn, cursor, statement, *args):
        if "FROM assistant_" in statement:
            statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", count)
    try:
        body = await view(app, conversation)
    finally:
        sa.event.remove(engine, "before_cursor_execute", count)
    assert len(body["items"]) == 10
    # The conversation, its messages, their drafts.
    assert len(statements) == 3


@pytest.mark.asyncio
async def test_a_running_turn(app, db):
    gate = Gate()
    equip(app, FakeLlm([TextDelta("working"), gate]))
    exchange = post_chat(app)
    await exchange.until("working")
    conversation = exchange.event("conversation")["conversation_id"]
    body = await view(app, conversation)
    assert body["turn_in_progress"] is True
    assert texts(body) == ["hello"]
    gate.opened.set()
    await exchange.finish()
    body = await view(app, conversation)
    assert body["turn_in_progress"] is False
    assert texts(body) == ["hello", "working"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("seconds", "in_progress"), [(110, True), (125, False)])
async def test_a_turn_whose_worker_died_is_not_in_progress_for_ever(
    app, db, seconds, in_progress
):
    conversation = add_conversation(
        db, active_turn_id=uuid.uuid4(), turn_started_at=ago(seconds=seconds)
    )
    assert (await view(app, conversation))["turn_in_progress"] is in_progress


@pytest.mark.asyncio
async def test_an_empty_conversation(app, db):
    conversation = add_conversation(db, title=None)
    body = await view(app, conversation)
    assert (body["title"], body["items"], body["has_more"]) == (None, [], False)


@pytest.mark.asyncio
async def test_looking_changes_nothing(app, db):
    conversation = add_conversation(db, updated_at=ago(days=80))
    before = read(db, Conversation, conversation)
    await view(app, conversation)
    assert read(db, Conversation, conversation) == before


# --- whose ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "me", [{"email": "else@example.com"}, {"tenant_id": OTHER_TENANT}], ids=str
)
async def test_someone_elses_conversation_does_not_exist(app, db, me):
    conversation = add_conversation(db)
    add_message(db, conversation, content="secret")
    equip(app, gateway=FakeGateway().on_json("GET", "/whoami", whoami(**me)))
    theirs = await get(app, conversation).finish()
    missing = await get(app, uuid.uuid4()).finish()
    malformed = await get(app, "not-a-uuid").finish()
    assert (theirs.status, theirs.code) == (404, "conversation_not_found")
    assert theirs.json() == missing.json() == malformed.json()
    assert missing.status == malformed.status == 404


@pytest.mark.asyncio
async def test_a_draft_of_someone_else_is_left_out(app, db):
    conversation = add_conversation(db)
    draft = add_draft(db, conversation, created_by="else@example.com")
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    add_message(db, conversation, content="after")
    assert texts(await view(app, conversation)) == ["after"]


@pytest.mark.asyncio
async def test_without_a_token(app, db):
    exchange = await Exchange(
        app, "GET", f"/assistant/conversations/{uuid.uuid4()}", token=None
    ).finish()
    assert (exchange.status, exchange.code) == (401, "unauthenticated")


# --- links ---------------------------------------------------------------------------

EDITOR = ["read:*", "write:*", "delete:*", "update:*", "execute:*"]


def stored_link(page, param, path, label="Open it", **extra) -> str:
    """A tool row's content as open_page stores it."""
    link = {"page": page, "param": param, "path": path, "label": label, **extra}
    return encode_result({"status": "link_ready", "link": link})


def add_link(db, conversation, content, call_id, name="open_page", asked_in=None):
    """The model's call, then its tool row; the call can be in another conversation."""
    call = {"id": call_id, "name": name, "arguments": "{}"}
    add_message(
        db, asked_in or conversation, "assistant", content="", tool_calls=[call]
    )
    return add_message(db, conversation, "tool", content=content, tool_call_id=call_id)


@pytest.mark.asyncio
async def test_a_stored_link_is_shown_where_its_tool_row_is(app, db):
    conversation = add_conversation(db)
    add_message(db, conversation, content="where is payments")
    row = add_link(
        db,
        conversation,
        stored_link("alerts_preset", "payments", "/alerts/payments", "Open Payments"),
        "call_1",
    )
    add_message(db, conversation, "assistant", content="Here.")
    body = await view(app, conversation)
    assert texts(body) == ["where is payments", "/alerts/payments", "Here."]
    item = body["items"][1]
    assert item == {
        "type": "link",
        "id": str(row),
        "page": "alerts_preset",
        "param": "payments",
        "path": "/alerts/payments",
        "label": "Open Payments",
        "created_at": item["created_at"],
    }


@pytest.mark.asyncio
async def test_a_link_is_read_leniently(app, db):
    """Keys it does not know are left out; the label is cleaned again."""
    conversation = add_conversation(db)
    add_link(
        db,
        conversation,
        stored_link("alerts_feed", None, "/alerts/feed", "Open\u202e it", extra=1),
        "call_1",
    )
    [item] = (await view(app, conversation))["items"]
    assert set(item) == {"type", "id", "page", "param", "path", "label", "created_at"}
    assert item["label"] == "Open it"


@pytest.mark.asyncio
async def test_a_link_the_user_may_no_longer_open_is_left_out(app, db):
    conversation = add_conversation(db)
    add_link(
        db,
        conversation,
        stored_link("dashboard", "NOC", "/dashboard/NOC"),
        "call_1",
    )
    add_message(db, conversation, "assistant", content="after")
    # whoami's role is noc: keep-ui would send them back to the feed.
    assert texts(await view(app, conversation)) == ["after"]
    editor = whoami(role="editor", scopes=EDITOR)
    equip(app, gateway=FakeGateway().on_json("GET", "/whoami", editor))
    assert texts(await view(app, conversation)) == ["/dashboard/NOC", "after"]


@pytest.mark.asyncio
async def test_only_open_page_rows_that_made_a_link(app, db):
    conversation = add_conversation(db)
    elsewhere = add_conversation(db)
    feed = stored_link("alerts_feed", None, "/alerts/feed")
    # Mentions link_ready, but does not lead with it.
    add_link(
        db,
        conversation,
        encode_result({"status": "found", "rows": ["link_ready"]}),
        "call_1",
    )
    # Leads with it, but answers a call of another tool.
    add_link(db, conversation, feed, "call_2", name="list_presets")
    # Leads with it, but the call is in another conversation.
    add_link(db, conversation, feed, "call_3", asked_in=elsewhere)
    # A path this version would not build.
    add_link(db, conversation, stored_link("alerts_feed", None, "//evil"), "call_4")
    add_link(
        db, conversation, stored_link("alerts_preset", "x", "/alerts/feed"), "call_5"
    )
    add_link(
        db,
        conversation,
        stored_link("alerts_preset", "Payments", "/alerts/payments"),
        "call_6",
    )
    add_link(db, conversation, stored_link("alerts_feed", None, "/alerts/feed", 7),
             "call_7")  # fmt: skip
    add_link(db, conversation, stored_link("alerts_feed", None, "/alerts/feed", " "),
             "call_8")  # fmt: skip
    # Not JSON, or not the shape, after the prefix.
    add_link(db, conversation, LINK_RESULT_PREFIX + "{", "call_9")
    add_link(db, conversation, LINK_RESULT_PREFIX + '"link":[1]}', "call_10")
    add_link(db, conversation, LINK_RESULT_PREFIX + '"link":{}}', "call_11")
    add_message(db, conversation, "assistant", content="after")
    assert texts(await view(app, conversation)) == ["after"]
    add_link(db, conversation, feed, "call_12")
    assert texts(await view(app, conversation)) == ["after", "/alerts/feed"]
