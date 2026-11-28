"""What users, alerts, the model and exceptions say stays out of logs, error bodies and
the frames of a stream that are not made to carry it.

The assistant's nested calls are answered by a double: the gateway's own routes log what
they always did.
"""
import json
import logging
import uuid

import httpx
import pytest
import sqlalchemy as sa

from src.services.assistant.bl import conversations_bl, drafts_bl
from src.services.assistant.exceptions import VALIDATION_MESSAGES
from src.services.assistant.llm.client import (
    Finished,
    LlmError,
    ToolCallComplete,
    Usage,
)
from src.services.assistant.models.db import Draft, Message
from tests.assistant.fakes import TOKEN, FakeGateway, alert, query_result, whoami
from tests.assistant.logs import rendered
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    add_message,
    ago,
    change,
    draft_payload,
    read,
)
from tests.assistant.running import (
    Exchange,
    FakeLlm,
    Keep,
    calls,
    equip,
    post_chat,
    post_confirm,
    says,
)

MESSAGE = "canary-message-61f0"
PAGE = "canary-page-27ab"
ALERT = "canary-alert-9d3e"
FINGERPRINT = "canary-fingerprint-c4d1"
CEL = "canary-cel-5b72"
NOTE = "canary-note-e08c"
ANSWER = "canary-answer-1f95"
EXCEPTION = "canary-exception-84aa"
SEARCH = "canary-search-cel-3a9d"
LINK = "canary-link-label-7c2f"
NAME = "canary-name-d61e"
SECRET = TOKEN.split(".")[-1]

CONTENT = (
    MESSAGE, PAGE, ALERT, FINGERPRINT, CEL, NOTE, ANSWER, SECRET, SEARCH, LINK, NAME
)  # fmt: skip
QUERY = ("POST", "/alerts/query")
COUNT = ("POST", "/alerts/query/count")
WRITE = ("POST", "/alerts/batch_enrich")


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def records(monkeypatch, caplog):
    """Every record of every logger, propagating or not, at any level."""
    caplog.set_level(logging.DEBUG)
    captured = []
    handle = logging.Logger.handle

    def capture(self, record):
        captured.append(record)
        handle(self, record)

    monkeypatch.setattr(logging.Logger, "handle", capture)
    return captured


def assert_not_logged(records, capfd) -> None:
    assert records
    for text in (*CONTENT, EXCEPTION):
        assert [r.name for r in records if text in rendered(r)] == [], text
    printed = "".join(capfd.readouterr())
    assert [text for text in (*CONTENT, EXCEPTION) if text in printed] == []


def telling_alert(fingerprint: str = FINGERPRINT) -> dict:
    return alert(
        fingerprint,
        name=f"name {ALERT}",
        description=f"description {ALERT}",
        service=f"service {ALERT}",
        source=[f"source {ALERT}"],
        note=f"note {ALERT}",
        incident=f"incident {ALERT}",
        labels={"team": ALERT},
    )


def gateway() -> FakeGateway:
    return (
        FakeGateway()
        .on_json("GET", "/whoami", whoami())
        .on_json("GET", "/alerts/facets/fields", ["name", f"labels.{CEL}"])
        .on_json(*QUERY, query_result(telling_alert()))
        .on_json(*COUNT, 99)
    )


def by_cel() -> dict:
    return {
        "fingerprints": None,
        "cel": f"name == '{CEL}'",
        "status": "acknowledged",
        "note": NOTE,
        "dispose_on_new_alert": False,
    }


def ask(app, **context) -> Exchange:
    return post_chat(
        app, MESSAGE, page=f"/{PAGE}", selected_fingerprints=[FINGERPRINT], **context
    )


def view(app, conversation) -> Exchange:
    return Exchange(app, "GET", f"/assistant/conversations/{conversation}")


def discard(app, draft) -> Exchange:
    return Exchange(app, "POST", f"/assistant/drafts/{draft}/discard")


def keep() -> Keep:
    return Keep({FINGERPRINT: "firing"})


def add_telling_draft(db, **values):
    conversation = values.pop("conversation", None) or add_conversation(
        db, title=MESSAGE
    )
    payload = draft_payload(FINGERPRINT, note=NOTE)
    payload["alerts"][0]["name"] = ALERT
    draft = add_draft(db, conversation, payload=payload, **values)
    add_message(db, conversation, content=MESSAGE)
    call = {"id": "call_1", "type": "function", "function": {"name": CEL}}
    add_message(db, conversation, "assistant", content=ANSWER, tool_calls=[call])
    add_message(
        db, conversation, "tool", content=NOTE, tool_call_id="call_1", draft_id=draft
    )
    return draft


# --- when it works ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chat(app, db, records, capfd):
    llm = FakeLlm(calls(by_cel(), text=ANSWER), says(ANSWER))
    equip(app, llm, gateway())
    exchange = await ask(app).finish()
    # It is not because nothing of it was there.
    prompt = json.dumps([r.messages for r in llm.requests])
    for text in (MESSAGE, ALERT, FINGERPRINT):
        assert text in prompt, text
    assert PAGE not in prompt  # not one of Keep's pages
    for text in (ALERT, FINGERPRINT, CEL, NOTE, ANSWER):
        assert text in exchange.text, text
    assert_not_logged(records, capfd)
    # Only the frames that are made for it carry it.
    for name, data in exchange.events:
        carried = [text for text in CONTENT if text in json.dumps(data)]
        if name == "text_delta":
            assert carried == [ANSWER]
        elif name != "draft":
            assert carried == [], name


@pytest.mark.asyncio
async def test_the_search_box_reaches_the_model_only(app, db, records, capfd):
    llm = FakeLlm(says(ANSWER))
    equip(app, llm, gateway())
    cel = f'name == "{SEARCH}" && count < 5'
    exchange = await post_chat(
        app, MESSAGE, page="/alerts/feed", search_cel=cel
    ).finish()
    assert exchange.status == 200
    assert "search_cel" in json.dumps([r.messages for r in llm.requests])
    assert SEARCH in json.dumps([r.messages for r in llm.requests])
    assert SEARCH not in exchange.text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "search_cel",
    [f"{SEARCH}\n", f"{SEARCH}\x00", SEARCH * 100, f"{SEARCH}\u202e"],
    ids=["control", "nul", "long", "bidi"],
)
@pytest.mark.asyncio
async def test_a_search_box_that_is_refused_or_dropped(
    app, db, records, capfd, search_cel
):
    llm = FakeLlm(says(ANSWER))
    equip(app, llm, gateway())
    exchange = await post_chat(
        app, MESSAGE, page="/alerts/feed", search_cel=search_cel
    ).finish()
    assert exchange.status in (200, 400)
    assert SEARCH not in exchange.text
    assert SEARCH not in json.dumps([r.messages for r in llm.requests])
    assert_not_logged(records, capfd)


def link_gateway(**who) -> FakeGateway:
    return (
        gateway()
        .on_json("GET", "/whoami", whoami(**who))
        .on_json("GET", "/preset", [{"id": "p-1", "name": NAME, "static": False}])
        .on_json("GET", "/dashboard", [{"id": 1, "dashboard_name": NAME}])
    )


def open_page(page: str, label: str) -> dict:
    return {"page": page, "param": NAME, "label": label}


@pytest.mark.asyncio
async def test_a_link_label_and_name_reach_only_the_link(app, db, records, capfd):
    llm = FakeLlm(
        calls(open_page("alerts_preset", f"Open {LINK}"), name="open_page"),
        says(ANSWER),
    )
    equip(app, llm, link_gateway())
    exchange = await post_chat(app, MESSAGE, page="/alerts/feed").finish()
    link = exchange.event("link")
    assert LINK in link["label"] and NAME in link["path"]
    for name, data in exchange.events:
        if name != "link":
            assert [t for t in (LINK, NAME) if t in json.dumps(data)] == [], name
    conversation = exchange.event("conversation")["conversation_id"]
    shown = await view(app, conversation).finish()
    assert LINK in shown.text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "arguments",
    [
        open_page("dashboard", LINK),  # a NOC user
        open_page("alerts_preset", LINK * 10),  # too long
        {**open_page("alerts_preset", LINK), "param": f"{NAME}-missing"},
        {**open_page("alerts_preset", LINK), "param": f"{NAME}%2F"},
        {**open_page("alerts_preset", LINK), "page": NAME},
        {**open_page("alerts_preset", LINK), "path": f"/{NAME}"},
    ],
    ids=["noc", "long", "not-found", "unusable", "page", "path"],
)
@pytest.mark.asyncio
async def test_a_link_that_is_not_made(app, db, records, capfd, arguments):
    llm = FakeLlm(calls(arguments, name="open_page"), says("Sorry."))
    equip(app, llm, link_gateway())
    exchange = await post_chat(app, MESSAGE, page="/alerts/feed").finish()
    assert "link" not in exchange.names
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    conversation = exchange.event("conversation")["conversation_id"]
    shown = await view(app, conversation).finish()
    assert LINK not in shown.text and NAME not in shown.text
    assert_not_logged(records, capfd)


ASSIGNEE = "canary-assignee-0b6e@example.com"


@pytest.mark.asyncio
async def test_an_assignees_email_reaches_neither_the_model_nor_the_card(
    app, db, records, capfd
):
    """Selected, matched by a filter or named by fingerprint: an alert is projected
    before a prompt, a tool result or a draft sees it."""
    assigned = telling_alert()
    assigned.update(assignee=ASSIGNEE, enriched_by=ASSIGNEE)
    assigned["labels"] = {"owner": ASSIGNEE}
    by_fingerprint = {**by_cel(), "fingerprints": [FINGERPRINT], "cel": None}
    llm = FakeLlm(calls(by_cel(), by_fingerprint), says(ANSWER))
    equip(app, llm, gateway().on_json(*QUERY, query_result(assigned)))
    exchange = await ask(app).finish()
    assert exchange.names.count("draft") == 2
    prompt = json.dumps([r.messages for r in llm.requests])
    assert FINGERPRINT in prompt and ALERT in prompt
    assert ASSIGNEE not in prompt
    assert ASSIGNEE not in exchange.text
    conversation = uuid.UUID(exchange.event("conversation")["conversation_id"])
    with db.connect() as conn:
        stored = [
            dict(row)
            for model in (Draft, Message)
            for row in conn.execute(
                sa.select(model.__table__).where(
                    model.__table__.c.conversation_id == conversation
                )
            ).mappings()
        ]
    assert len(stored) == 7  # 2 drafts; the question, 2 answers, 2 tool rows
    assert ASSIGNEE not in json.dumps(stored, default=str)
    assert [r.name for r in records if ASSIGNEE in rendered(r)] == []
    assert ASSIGNEE not in "".join(capfd.readouterr())


def read_round(*named: tuple[str, dict]) -> list:
    """One round that calls each (tool, arguments)."""
    return [
        *(
            ToolCallComplete(f"call_{n}", name, json.dumps(arguments))
            for n, (name, arguments) in enumerate(named, 1)
        ),
        Usage(100, 10),
        Finished("tool_calls"),
    ]


@pytest.mark.asyncio
async def test_an_assignees_email_reaches_no_read_tool_result(app, db, records, capfd):
    """Searched, read by fingerprint, counted or asked for by its own field name."""
    assigned = telling_alert()
    assigned.update(assignee=ASSIGNEE, enriched_by=ASSIGNEE)
    assigned["labels"] = {"owner": ASSIGNEE, "team": ALERT}
    fields = ["name", "assignee", "enriched_by", "labels.owner", "labels.team"]
    rounds = read_round(
        ("search_alerts", {"cel": "", "limit": 5, "sort": None}),
        ("get_alerts", {"fingerprints": [FINGERPRINT]}),
        ("get_field_values", {"field": "assignee", "cel": None}),
        ("get_field_values", {"field": "enriched_by", "cel": None}),
        ("get_field_values", {"field": "labels.owner", "cel": None}),
        ("get_field_values", {"field": "labels.team", "cel": None}),
        ("count_alerts", {"cel": f"name == '{CEL}'"}),
    )
    llm = FakeLlm(rounds, says(ANSWER))
    fake = (
        gateway()
        .on_json("GET", "/alerts/facets/fields", fields)
        .on_json("GET", "/alerts/facets", [])
        .on_json(*QUERY, query_result(assigned))
    )
    equip(app, llm, fake)
    exchange = await ask(app).finish()
    finished = [
        (data["tool"], data["outcome"])
        for name, data in exchange.events
        if name == "tool_status" and data["phase"] == "finished"
    ]
    assert finished == [
        ("search_alerts", "found"),
        ("get_alerts", "found"),
        ("get_field_values", "error"),
        ("get_field_values", "error"),
        ("get_field_values", "found"),
        ("get_field_values", "found"),
        ("count_alerts", "found"),
    ]
    prompt = json.dumps([r.messages for r in llm.requests])
    assert FINGERPRINT in prompt and ALERT in prompt
    assert ASSIGNEE not in prompt
    assert ASSIGNEE not in exchange.text
    conversation = uuid.UUID(exchange.event("conversation")["conversation_id"])
    with db.connect() as conn:
        stored = [
            dict(row)
            for row in conn.execute(
                sa.select(Message.__table__).where(
                    Message.__table__.c.conversation_id == conversation
                )
            ).mappings()
        ]
    assert ASSIGNEE not in json.dumps(stored, default=str)
    assert [r.name for r in records if ASSIGNEE in rendered(r)] == []
    assert_not_logged(records, capfd)


@pytest.mark.asyncio
async def test_confirming_and_looking_at_it(app, db, records, capfd):
    equip(app, None, keep())
    draft = add_telling_draft(db)
    exchange = await post_confirm(app, draft).finish()
    assert exchange.json()["outcome"]["code"] == "applied"
    conversation = read(db, Draft, draft)["conversation_id"]
    shown = await view(app, conversation).finish()
    assert ALERT in shown.text and MESSAGE in shown.text
    assert_not_logged(records, capfd)


@pytest.mark.asyncio
async def test_reconciling(app, db, records, capfd):
    equip(app, None, keep())
    conversation = add_conversation(db, title=MESSAGE)
    draft = add_telling_draft(
        db, conversation=conversation, state="applying", updated_at=ago(minutes=2)
    )
    await view(app, conversation).finish()
    assert read(db, Draft, draft)["state"] == "failed"
    assert_not_logged(records, capfd)


@pytest.mark.asyncio
async def test_discarding(app, db, records, capfd):
    equip(app, None, keep())
    draft = add_telling_draft(db)
    exchange = await discard(app, draft).finish()
    assert exchange.status == 200
    assert_not_logged(records, capfd)


# --- a chat that fails ------------------------------------------------------------------


def invalid_cel() -> dict:
    return {
        "detail": {
            "code": "INVALID_CEL",
            "message": EXCEPTION,
            "diagnostics": [{"code": "SYNTAX_ERROR", "message": EXCEPTION}],
        }
    }


TOOL_FAILURES = {
    "invalid-cel": (QUERY, httpx.Response(400, json=invalid_cel())),
    "query-failed": (QUERY, httpx.Response(500, json={"message": EXCEPTION})),
    "refused": (QUERY, httpx.Response(403, json={"detail": EXCEPTION})),
    "no-session": (QUERY, httpx.Response(401, json={"detail": EXCEPTION})),
    "not-json": (QUERY, httpx.Response(200, text=EXCEPTION)),
    "dropped": (QUERY, httpx.ReadError(EXCEPTION)),
    "never-sent": (QUERY, httpx.ConnectError(EXCEPTION)),
    "bug": (QUERY, RuntimeError(EXCEPTION)),
    "too-many": (
        QUERY,
        httpx.Response(
            200,
            json=query_result(
                *(telling_alert(f"{FINGERPRINT}-{n}") for n in range(21))
            ),
        ),
    ),
    "none": (QUERY, httpx.Response(200, json=query_result())),
}


@pytest.mark.parametrize("failure", TOOL_FAILURES)
@pytest.mark.asyncio
async def test_a_lookup_that_fails(app, db, records, capfd, failure):
    route, answer = TOOL_FAILURES[failure]
    equip(app, FakeLlm(calls(by_cel()), says("Sorry.")), gateway().on(*route, answer))
    exchange = await ask(app).finish()
    assert "draft" not in exchange.names
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "arguments",
    [
        f"{{{NOTE}",
        {**by_cel(), "status": NOTE},
        {**by_cel(), "note": NOTE * 400},
        {**by_cel(), CEL: NOTE},
        {**by_cel(), "fingerprints": [FINGERPRINT]},
        {**by_cel(), "cel": [CEL]},
    ],
    ids=["not-json", "status", "note", "key", "both", "type"],
)
@pytest.mark.asyncio
async def test_arguments_that_make_no_sense(app, db, records, capfd, arguments):
    equip(app, FakeLlm(calls(arguments), says("Sorry.")), gateway())
    exchange = await ask(app).finish()
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(EXCEPTION),
        OSError(EXCEPTION),
        LlmError("rejected"),
        LlmError("unavailable"),
        Finished("content_filter"),
        Finished(EXCEPTION),
    ],
    ids=["bug", "os", "rejected", "unavailable", "filtered", "odd-reason"],
)
@pytest.mark.asyncio
async def test_a_model_that_fails(app, db, records, capfd, failure):
    equip(app, FakeLlm([failure]), gateway())
    exchange = await ask(app).finish()
    assert exchange.names[-1] == "done"
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    assert_not_logged(records, capfd)


@pytest.fixture()
def broken(monkeypatch):
    """Makes a database function fail with the text of a real driver's error."""

    def break_(module, function: str) -> None:
        def fail(*args, **kwargs):
            raise sa.exc.OperationalError(
                f"UPDATE {EXCEPTION}",
                {"content": MESSAGE, "payload": ALERT},
                Exception(f"{EXCEPTION} DETAIL: {NOTE}"),
            )

        monkeypatch.setattr(module, function, fail)

    return break_


@pytest.mark.parametrize("function", ["start_turn", "add_messages", "end_turn"])
@pytest.mark.asyncio
async def test_a_database_that_fails(app, db, records, capfd, broken, function):
    broken(conversations_bl, function)
    equip(app, FakeLlm(calls(by_cel()), says(ANSWER)), gateway())
    exchange = await ask(app).finish()
    for text in (MESSAGE, PAGE, SECRET, EXCEPTION):
        assert text not in exchange.text, text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"detail": EXCEPTION}),
        httpx.Response(403, json={"detail": EXCEPTION}),
        httpx.Response(500, text=EXCEPTION),
        httpx.Response(200, text=EXCEPTION),
        httpx.Response(200, json=[EXCEPTION]),
        httpx.Response(200, json={"tenant_id": 5, "email": EXCEPTION}),
        httpx.ReadTimeout(EXCEPTION),
        RuntimeError(EXCEPTION),
    ],
    ids=["401", "403", "500", "text", "list", "odd", "timeout", "bug"],
)
@pytest.mark.parametrize("what", ["chat", "confirm", "discard", "view"])
@pytest.mark.asyncio
async def test_an_identity_that_fails(app, db, records, capfd, answer, what):
    equip(app, FakeLlm(), gateway().on("GET", "/whoami", answer))
    exchange = {
        "chat": lambda: ask(app),
        "confirm": lambda: post_confirm(app, uuid.uuid4()),
        "discard": lambda: discard(app, uuid.uuid4()),
        "view": lambda: view(app, uuid.uuid4()),
    }[what]()
    await exchange.finish()
    assert exchange.status in (401, 403, 502, 503)
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "body",
    [
        {"message": MESSAGE * 300, "context": {}},
        {"message": {"text": MESSAGE}, "context": {}},
        {"message": MESSAGE, "context": {"page": PAGE * 100}},
        {"message": MESSAGE, "context": {"selected_fingerprints": [f"{ALERT}\x00"]}},
        {"message": MESSAGE, "conversation_id": NOTE, "context": {}},
        {"message": MESSAGE, "context": {}, CEL: ANSWER},
    ],
    ids=["long", "type", "page", "fingerprint", "conversation", "extra"],
)
@pytest.mark.asyncio
async def test_a_request_that_makes_no_sense(app, db, records, capfd, body):
    equip(app, FakeLlm(), gateway())
    exchange = await Exchange(app, "POST", "/assistant/chat", body).finish()
    assert exchange.status in (200, 400, 404)
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    if exchange.status == 400:
        # pydantic's own text can quote the input.
        answer = json.loads(exchange.text)
        texts = {answer["detail"], *(error["message"] for error in answer["errors"])}
        assert texts <= set(VALIDATION_MESSAGES.values())
    assert_not_logged(records, capfd)


# --- a Confirm that fails ---------------------------------------------------------------

FAILURES = {
    "refused": httpx.Response(403, json={"detail": EXCEPTION}),
    "rejected": httpx.Response(400, json={"detail": EXCEPTION}),
    "invalid": httpx.Response(422, json={"detail": EXCEPTION}),
    "error": httpx.Response(500, json={"message": EXCEPTION}),
    "not-json": httpx.Response(200, text=EXCEPTION),
    "dropped": httpx.RemoteProtocolError(EXCEPTION),
    "timeout": httpx.ReadTimeout(EXCEPTION),
    "never-sent": httpx.ConnectError(EXCEPTION),
    "bug": RuntimeError(EXCEPTION),
}


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("route", [QUERY, WRITE], ids=["look", "write"])
@pytest.mark.asyncio
async def test_a_confirm_that_fails(app, db, records, capfd, route, failure):
    equip(app, None, keep().on(*route, FAILURES[failure]))
    draft = add_telling_draft(db)
    exchange = await post_confirm(app, draft).finish()
    for text in (MESSAGE, PAGE, SECRET, EXCEPTION):
        assert text not in exchange.text, text
    if exchange.status != 200:
        assert "draft" not in exchange.json() and ALERT not in exchange.text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(status, json={"detail": EXCEPTION, "status": EXCEPTION})
        for status in (400, 403, 404, 409, 422, 500, 503)
    ],
    ids=lambda answer: str(answer.status_code),
)
@pytest.mark.asyncio
async def test_the_body_of_a_refused_write_goes_nowhere(app, db, records, capfd, answer):
    """Neither into the log, nor the stored outcome, nor what the conversation shows."""
    equip(app, None, keep().on(*WRITE, answer))
    draft = add_telling_draft(db)
    conversation = read(db, Draft, draft)["conversation_id"]
    exchange = await post_confirm(app, draft).finish()
    assert EXCEPTION not in exchange.text
    assert EXCEPTION not in json.dumps(read(db, Draft, draft)["outcome"])
    shown = await view(app, conversation).finish()
    assert shown.status == 200 and EXCEPTION not in shown.text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize("function", ["claim", "resolve", "revert", "keep_applying"])
@pytest.mark.parametrize("failure", ["refused", "never-sent", "timeout"])
@pytest.mark.asyncio
async def test_a_confirm_whose_database_fails(
    app, db, records, capfd, broken, function, failure
):
    draft = add_telling_draft(db)
    broken(drafts_bl, function)
    equip(app, None, keep().on(*WRITE, FAILURES[failure]))
    exchange = await post_confirm(app, draft).finish()
    assert EXCEPTION not in exchange.text
    if exchange.status >= 500:
        assert ALERT not in exchange.text
    assert_not_logged(records, capfd)


@pytest.mark.parametrize("function", ["claim_look", "resolve"])
@pytest.mark.asyncio
async def test_a_reconcile_whose_database_fails(
    app, db, records, capfd, broken, function
):
    conversation = add_conversation(db, title=MESSAGE)
    add_telling_draft(
        db, conversation=conversation, state="applying", updated_at=ago(minutes=2)
    )
    broken(drafts_bl, function)
    equip(app, None, keep())
    shown = await view(app, conversation).finish()
    assert shown.status == 200 and EXCEPTION not in shown.text
    assert_not_logged(records, capfd)


@pytest.mark.asyncio
async def test_a_draft_that_cannot_be_read(app, db, records, capfd):
    """Something else wrote it."""
    draft = add_telling_draft(db)
    change(db, Draft, draft, payload={"alerts": EXCEPTION, "note": NOTE})
    equip(app, None, keep())
    exchange = await post_confirm(app, draft).finish()
    assert exchange.status >= 500
    for text in (*CONTENT, EXCEPTION):
        assert text not in exchange.text, text
    assert_not_logged(records, capfd)
