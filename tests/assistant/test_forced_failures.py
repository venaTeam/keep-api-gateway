"""No exception text in any log or body, whatever goes wrong.

Each scratch route fails with the canary in its exception text. Without the route
class the gateway's catch-all would log and return that text; the routes set
`request.state.trace_id`, which it needs for that, so that the leak is a real one.
"""
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Optional

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.engine.url import make_url

from src.config import consts
from src.services.assistant.api.route import AssistantRoute
from src.services.assistant.bl import drafts_bl
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import ERRORS, error_body
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.client import (
    WriteRoute,
    classify_object_write,
    object_id_of,
)
from src.services.assistant.kinds import registry
from src.services.assistant.kinds.base import (
    ConfirmContext,
    ConfirmPlan,
    Decision,
    DraftKind,
    Sent,
    Step,
    WriteRequest,
)
from src.services.assistant.kinds.registry import KINDS
from src.services.assistant.models.api.draft import (
    ChangeAlertStatusPayload,
    DraftObject,
    DraftOutcome,
    DraftView,
)
from src.services.assistant.models.db import Draft
from tests.assistant.fakes import TOKEN
from tests.assistant.golden_stream import uid
from tests.assistant.logs import rendered
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    add_message,
    ago,
    change,
    read,
)
from tests.assistant.running import Exchange, Keep, equip, post_confirm

CANARY = "canary-9f2c7d"
INTERNAL = error_body("internal", ERRORS["internal"].message)
NOT_READY = error_body("not_ready", ERRORS["not_ready"].message)


class Answer(BaseModel):
    count: int


def set_trace_id(request: Request):
    request.state.trace_id = "trace-1"


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    leaky = {"dependencies": [Depends(set_trace_id)]}

    @assistant_router.get("/scratch/bug", **leaky)
    async def bug():
        raise RuntimeError(CANARY)

    @assistant_router.get("/scratch/bug-in-a-thread", **leaky)
    def bug_in_a_thread():
        raise KeyError(CANARY)

    @assistant_router.get("/scratch/chained-bug", **leaky)
    async def chained_bug():
        try:
            raise ValueError(CANARY)
        except ValueError as exc:
            raise RuntimeError("x") from exc

    async def failing():
        raise RuntimeError(CANARY)

    @assistant_router.get("/scratch/bug-in-a-dependency", **leaky)
    async def bug_in_a_dependency(value=Depends(failing)):
        return {}

    @assistant_router.get("/scratch/bad-answer", response_model=Answer, **leaky)
    async def bad_answer():
        return {"count": CANARY}

    @assistant_router.get("/scratch/http-error", **leaky)
    async def http_error():
        raise HTTPException(500, CANARY)

    @assistant_router.get("/scratch/query", **leaky)
    def query():
        with db_core.get_session() as session:
            session.execute(sa.text("SELECT 1"))
        return {}

    @assistant_router.get("/scratch/slow-query", **leaky)
    def slow_query():
        with db_core.get_session() as session:
            session.execute(sa.text(f"SELECT pg_sleep(30), '{CANARY}'"))

    @assistant_router.get("/scratch/bad-query", **leaky)
    def bad_query():
        with db_core.get_session() as session:
            session.execute(sa.text("SELECT CAST(:v AS int)"), {"v": CANARY})

    return make_app()


@pytest.fixture()
def client(app):
    return TestClient(
        app, headers={"Authorization": TOKEN}, raise_server_exceptions=False
    )


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


def count(site: str) -> float:
    return metrics.internal_errors_total.labels(site=site)._value.get()


def assert_hidden(response, records, *texts):
    assert records
    for text in texts:
        assert text not in response.text
        assert not [r.name for r in records if text in rendered(r)]


def unhandled(records) -> list[logging.LogRecord]:
    return [
        r for r in records if r.getMessage() == "assistant: unhandled request error"
    ]


def test_the_scratch_routes_have_the_route_class(app):
    scratch = [r for r in app.routes if r.path.startswith("/assistant/scratch/")]
    assert len(scratch) == 9
    assert all(isinstance(route, AssistantRoute) for route in scratch)


# --- bugs ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "error_type", "frame"),
    [
        ("bug", "RuntimeError", "bug"),
        ("bug-in-a-thread", "KeyError", "bug_in_a_thread"),
        ("chained-bug", "RuntimeError", "chained_bug"),
        ("bug-in-a-dependency", "RuntimeError", "failing"),
        ("bad-answer", "ResponseValidationError", "serialize_response"),
    ],
)
def test_a_raised_bug(client, schema_ready, records, path, error_type, frame):
    before = count("request")
    response = client.get(f"/assistant/scratch/{path}")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert_hidden(response, records, CANARY)
    (record,) = unhandled(records)
    assert record.error_type == error_type
    assert record.frames[-1].endswith(f" {frame}")
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.exc_info is None
    assert count("request") == before + 1


def test_the_text_of_an_http_error(client, schema_ready, records):
    response = client.get("/assistant/scratch/http-error")
    assert response.status_code == 500
    assert response.json() == error_body("http_error", ERRORS["http_error"].message)
    assert_hidden(response, records, CANARY)


# --- the database --------------------------------------------------------------


def test_a_statement_timeout(client, db_engine, set_env, records):
    set_env("ASSISTANT_DB_STATEMENT_TIMEOUT_MS", 300)
    db_core.dispose_engine()
    response = client.get("/assistant/scratch/slow-query")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert_hidden(response, records, CANARY, "statement timeout")
    (record,) = unhandled(records)
    assert record.error_type == "OperationalError"


def test_a_rejected_value(client, db_engine, records):
    response = client.get("/assistant/scratch/bad-query")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    # Postgres quotes the value in its own error line.
    assert_hidden(response, records, CANARY)
    (record,) = unhandled(records)
    assert record.error_type == "DataError"


@pytest.fixture()
def one_connection(db_engine, set_env):
    for name, value in (("SIZE", 1), ("TIMEOUT", 0.2)):
        set_env(f"ASSISTANT_DB_POOL_{name}", value)
    set_env("ASSISTANT_DB_MAX_OVERFLOW", 0)
    db_core.dispose_engine()


def test_an_exhausted_pool(client, one_connection, records):
    assert client.get("/assistant/scratch/query").status_code == 200
    with db_core.get_engine().connect():
        response = client.get("/assistant/scratch/query")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert_hidden(response, records, "QueuePool")
    (record,) = unhandled(records)
    assert record.error_type == "TimeoutError"


def test_an_exhausted_pool_during_the_schema_check(client, one_connection, records):
    before = count("readiness_database")
    with db_core.get_engine().connect():
        response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert_hidden(response, records, "QueuePool")
    assert count("readiness_database") == before + 1
    # A failure is not cached.
    assert client.get("/assistant/scratch/query").status_code == 200


def test_a_failed_connection(client, assistant_dsn, monkeypatch, records):
    dsn = make_url(assistant_dsn).set(database=CANARY, password="secret-password")
    monkeypatch.setattr(
        consts, "DB_CONNECTION_STRING", dsn.render_as_string(hide_password=False)
    )
    before = count("readiness_database")
    response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert_hidden(response, records, CANARY, "secret-password")
    assert count("readiness_database") == before + 1


def test_no_database_at_all(client, monkeypatch, records):
    monkeypatch.setattr(consts, "DB_CONNECTION_STRING", f"sqlite:///{CANARY}.db")
    response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert_hidden(response, records, CANARY)


def test_a_missing_table_is_not_ready(client, db_engine, monkeypatch, records):
    monkeypatch.setattr(
        db_core,
        "REQUIRED_SCHEMA",
        {**db_core.REQUIRED_SCHEMA, "assistant_absent": frozenset({"id"})},
    )
    before = count("readiness_schema")
    response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert count("readiness_schema") == before + 1
    (record,) = [r for r in records if hasattr(r, "missing")]
    assert record.missing == ["assistant_absent"]


# --- a failure after a sent write ------------------------------------------------------
# A kind of two object writes. Whatever fails after the first was sent, what it made is
# kept on the draft, which stays applying, and a later look at the conversation
# finishes it with that object.

MADE = (uid(901), uid(902))
THINGS = "two_things"


def a_thing(body) -> None:
    if not isinstance(body.get("name"), str):
        raise ValueError("name")


THING_ROW = WriteRoute(
    method="POST",
    path="/things",
    path_params=(),
    query=MappingProxyType({}),
    allowed_keys=frozenset({"name"}),
    check_body=a_thing,
    classify=classify_object_write,
    object_id=object_id_of,
    timeout_seconds=5.0,
)


def thing_outcome(code: str, result: str, made: Optional[str], **values) -> DraftOutcome:
    return DraftOutcome(
        **{
            "code": code,
            "message": f"text of {code}",
            "gateway_result": result,
            "applied_count": 0,
            "failed_count": 0,
            "unconfirmed_count": 0,
            "skipped_count": 0,
            "not_found_count": 0,
            "results": [],
            "finished_at": datetime.now(timezone.utc),
            "object": None if made is None else DraftObject(type="preset", id=made),
            **values,
        }
    )


def last_made(sent: Sequence[Sent]) -> Optional[str]:
    made = [s.answer.object_id for s in sent if s.answer.object_id]
    return made[-1] if made else None


class Things(DraftKind):
    name = THINGS
    payload_model = ChangeAlertStatusPayload
    scope_hint = frozenset({"write:preset"})

    def __init__(self, *failing: str):
        # Where it fails: after, look_after, pending, stopped, serialization.
        self.failing = set(failing)
        self.reconciled: list = []

    def write_scope(self, payload: BaseModel) -> str:
        return "write:preset"

    def fail(self, where: str) -> None:
        if where in self.failing:
            raise RuntimeError(CANARY)

    def plan(self, ctx: ConfirmContext) -> ConfirmPlan:
        async def first() -> Step:
            return WriteRequest("thing", {"name": "one"})

        async def after(sent: Sequence[Sent]) -> Step:
            if len(sent) == 1:
                self.fail("after")
                if "look_after" in self.failing:
                    await ctx.reader.fetch_alerts_by_fingerprints(["fp-1"])
                return WriteRequest("thing", {"name": "two"})
            if "serialization" in self.failing:
                stored = thing_outcome("applied", "ok", last_made(sent))
                return "applied", DraftOutcome.construct(
                    **{**stored.dict(), "code": CANARY}
                )
            return "applied", thing_outcome("applied", "ok", last_made(sent))

        def pending(sent: Sequence[Sent]) -> DraftOutcome:
            self.fail("pending")
            return thing_outcome(
                "outcome_pending", "unknown", last_made(sent), finished_at=None
            )

        def stopped(sent: Sequence[Sent]) -> Decision:
            self.fail("stopped")
            return "partially_applied", thing_outcome(
                "partially_applied", "ok", last_made(sent)
            )

        return ConfirmPlan(
            rows=("thing", "thing"),
            first=first,
            after=after,
            pending=pending,
            stopped=stopped,
        )

    async def reconcile(self, reader, view, confirm_state) -> Optional[Decision]:
        """By what the draft kept: its object, else the ids of its state."""
        self.reconciled.append((view, confirm_state))
        made = [i for i in (confirm_state or {}).get("object_ids", []) if i]
        if view.outcome is not None and view.outcome.object is not None:
            made.append(view.outcome.object.id)
        if not made:
            return "failed", thing_outcome("outcome_unknown", "unknown", None)
        return "applied", thing_outcome("applied", "ok", made[-1])

    def close_unknown(self, payload, stored, confirm_state) -> Decision:
        return "failed", thing_outcome("outcome_unknown", "unknown", None)

    def event_text(self, draft: DraftView, outcome: DraftOutcome) -> str:
        return f"draft {draft.id} ({draft.kind}): applied."

    def discarded_text(self, draft: DraftView, reason: Optional[str]) -> str:
        return f"draft {draft.id} ({draft.kind}): discarded by the user."


@pytest.fixture()
def keep(monkeypatch):
    made = iter(MADE)

    async def a_thing_made(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": next(made)})

    monkeypatch.setattr(
        client_module,
        "WRITE_ROUTES",
        MappingProxyType({**client_module.WRITE_ROUTES, "thing": THING_ROW}),
    )
    monkeypatch.setattr(
        client_module,
        "_WRITE_TIMEOUTS",
        MappingProxyType({**client_module._WRITE_TIMEOUTS, "thing": 5.0}),
    )
    return Keep({"fp-1": "firing"}).on("POST", "/things", a_thing_made)


@pytest.fixture()
def conversation(clean_db):
    return add_conversation(clean_db, updated_at=ago(days=3))


def with_kind(monkeypatch, kind: Things) -> Things:
    monkeypatch.setattr(registry, "KINDS", MappingProxyType({**KINDS, THINGS: kind}))
    return kind


async def confirmed_and_looked(app, db, keep, conversation, kind) -> tuple:
    """Confirm fails; then a look at the conversation. The row in between, and the
    draft the look shows."""
    equip(app, gateway=keep)
    draft = add_draft(db, conversation, "fp-1", kind=THINGS)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal"), exchange.text
    assert CANARY not in exchange.text
    row = read(db, Draft, draft)
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    change(db, Draft, draft, updated_at=ago(seconds=31))
    viewed = await Exchange(
        app, "GET", f"/assistant/conversations/{conversation}"
    ).finish()
    assert viewed.status == 200, viewed.text
    [shown] = [i["draft"] for i in viewed.json()["items"] if i["type"] == "draft"]
    return row, shown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "writes", "made", "kept"),
    [
        # After the first write came back: the plan's pending outcome.
        (("after",), 1, MADE[0], [MADE[0]]),
        (("look_after",), 1, MADE[0], [MADE[0]]),
        # The second write's answer is not known, and pending() fails: stopped().
        (("pending",), 2, MADE[0], [MADE[0], None]),
        # The result of both cannot be stored: the plan's pending outcome.
        (("serialization",), 2, MADE[1], [MADE[0], MADE[1]]),
    ],
    ids=["in-after", "in-the-look-after", "in-pending", "in-serialization"],
)
async def test_a_failure_after_a_write_keeps_what_it_made(
    app, clean_db, keep, conversation, monkeypatch, records, failing, writes, made, kept
):
    kind = with_kind(monkeypatch, Things(*failing))
    if "look_after" in failing:

        async def a_failing_look(*args, **kwargs):
            raise RuntimeError(CANARY)

        monkeypatch.setattr(
            client_module.GatewayClient,
            "fetch_alerts_by_fingerprints",
            a_failing_look,
        )
    if "pending" in failing:
        answers = iter([None, httpx.Response(502)])

        async def second_unknown(request):
            answer = next(answers)
            if answer is not None:
                return answer
            return httpx.Response(200, json={"id": MADE[0]})

        keep.on("POST", "/things", second_unknown)
    before = count("confirm_after_write")
    row, shown = await confirmed_and_looked(app, clean_db, keep, conversation, kind)
    assert count("confirm_after_write") == before + 1
    assert len([r for r in keep.requests if r.url.path == "/things"]) == writes
    assert row["state"] == "applying"
    assert row["outcome"]["object"]["id"] == made
    state = row["outcome"]["confirm_state"]
    assert (state["step"], state["object_ids"]) == (1, kept)
    # The look finished it with what was kept.
    assert (shown["state"], shown["outcome"]["object"]["id"]) == ("applied", made)
    [(view, given)] = kind.reconciled
    assert given == state
    assert_hidden_text(records, CANARY)


@pytest.mark.asyncio
async def test_when_the_kind_cannot_say_the_object_ids_stay(
    app, clean_db, keep, conversation, monkeypatch, records
):
    """after(), pending() and stopped() all fail: an outcome that claims nothing, with
    the ids of what came back."""
    kind = with_kind(monkeypatch, Things("after", "pending", "stopped"))
    row, shown = await confirmed_and_looked(app, clean_db, keep, conversation, kind)
    stored = row["outcome"]
    assert row["state"] == "applying"
    assert (stored["code"], stored["gateway_result"], stored["finished_at"]) == (
        "outcome_pending",
        "unknown",
        None,
    )
    assert "object" not in stored
    assert stored["confirm_state"]["object_ids"] == [MADE[0]]
    assert (shown["state"], shown["outcome"]["object"]["id"]) == ("applied", MADE[0])
    assert_hidden_text(records, CANARY)


@pytest.mark.asyncio
async def test_a_write_that_never_came_back(
    app, clean_db, keep, conversation, monkeypatch, records
):
    """The second write was handed to the client, which failed: nothing of the plan
    describes it, so only the generic outcome is kept, with the first one's id."""
    kind = with_kind(monkeypatch, Things())
    write = client_module.GatewayClient.write
    calls = []

    async def second_fails(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError(CANARY)
        return await write(self, *args, **kwargs)

    monkeypatch.setattr(client_module.GatewayClient, "write", second_fails)
    row, shown = await confirmed_and_looked(app, clean_db, keep, conversation, kind)
    stored = row["outcome"]
    assert (stored["code"], stored["gateway_result"]) == ("outcome_pending", "unknown")
    assert "object" not in stored
    # Write 2 may have been sent; only write 1 came back.
    state = stored["confirm_state"]
    assert (state["step"], state["object_ids"]) == (1, [MADE[0]])
    assert (shown["state"], shown["outcome"]["object"]["id"]) == ("applied", MADE[0])
    assert_hidden_text(records, CANARY)


@pytest.mark.asyncio
async def test_what_cannot_be_kept_is_logged_without_its_text(
    app, clean_db, keep, conversation, monkeypatch, records
):
    """The database fails too: the draft keeps its last checkpoint, and both
    failures are counted."""
    kind = with_kind(monkeypatch, Things("after"))
    checkpoint = drafts_bl.checkpoint

    def fails_after_the_first_two(*args, **kwargs):
        if len(calls) >= 2:
            raise RuntimeError(CANARY)
        calls.append(args)
        return checkpoint(*args, **kwargs)

    calls: list = []
    monkeypatch.setattr(drafts_bl, "checkpoint", fails_after_the_first_two)
    before = count("confirm_after_write")
    row, _ = await confirmed_and_looked(app, clean_db, keep, conversation, kind)
    assert count("confirm_after_write") == before + 2
    assert row["state"] == "applying"
    # The checkpoint before write 2: what write 1 made, and its id.
    assert row["outcome"]["object"]["id"] == MADE[0]
    assert row["outcome"]["confirm_state"]["object_ids"] == [MADE[0]]
    assert_hidden_text(records, CANARY)


def assert_hidden_text(records, text: str) -> None:
    assert records
    assert not [r.name for r in records if text in rendered(r)]
