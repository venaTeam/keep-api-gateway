"""The body of a Confirm: what only the browser can compute, checked against what the
draft's kind takes, before anything else happens to the draft."""
import uuid

import pydantic
import pytest

from src.services.assistant.exceptions import (
    VALIDATION_MESSAGES,
    AssistantError,
    validation_error,
)
from src.services.assistant.kinds import alert_status
from src.services.assistant.models.api.confirm import (
    MAX_DERIVED_BYTES,
    ConfirmRequest,
    DerivedInput,
    SqlQueryInput,
    derived_bytes,
)
from src.services.assistant.models.db import Draft
from tests.assistant import golden_stream
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.fakes import whoami
from tests.assistant.rows import add_conversation, add_draft, ago, read
from tests.assistant.running import Keep, equip, post_confirm

FIRING = {"fp-1": "firing", "fp-2": "firing"}


def derived(**changes) -> dict:
    value = golden_stream.DERIVED.dict()
    sql_query = {**value["sql_query"], **changes.pop("sql_query", {})}
    return {**value, "sql_query": sql_query, **changes}


def derived_of_size(size: int) -> dict:
    """Exactly `size` bytes, as the browser counts them."""
    params = {f"p_{n}": "v" * 500 for n in range(110)}
    value = derived(sql_query={"sql": "", "params": params})
    value["sql_query"]["sql"] = "s" * (size - derived_bytes(value))
    assert derived_bytes(value) == size
    return value


# --- the shapes, at each limit and one past it ----------------------------------


@pytest.mark.parametrize(
    "value",
    [
        derived(),
        derived(cel_roundtrip="c" * 4000),
        derived(sql_query={"sql": "s" * 20000}),
        derived(sql_query={"params": {}}),
        derived(sql_query={"params": {f"k_{n}": n for n in range(200)}}),
        derived(sql_query={"params": {"k" * 200: "v"}}),
        derived(sql_query={"params": {"labels.team_1": "v" * 500}}),
        derived(sql_query={"params": {"équipe_1": "é" * 500, "a.b-c/d_2": "x"}}),
        derived(sql_query={"params": {"a_1": 1, "b_1": 1.5, "c_1": True, "d_1": ""}}),
        derived_of_size(MAX_DERIVED_BYTES),
    ],
    ids=[
        "golden",
        "cel_roundtrip",
        "sql",
        "no-params",
        "params",
        "key",
        "value",
        "not-ascii",
        "value-types",
        "bytes",
    ],
)
def test_what_is_taken(value):
    assert DerivedInput.parse_obj(value).dict() == value


@pytest.mark.parametrize(
    "value",
    [
        derived(cel_roundtrip="c" * 4001),
        derived(cel_roundtrip=""),
        derived(cel_roundtrip=5),
        derived(cel_roundtrip="a\x00b"),
        derived(sql_query={"sql": "s" * 20001}),
        derived(sql_query={"sql": ""}),
        derived(sql_query={"sql": 5}),
        derived(sql_query={"sql": "a\ud800b"}),
        derived(sql_query={"params": {f"k_{n}": n for n in range(201)}}),
        derived(sql_query={"params": {"k" * 201: "v"}}),
        derived(sql_query={"params": {"": "v"}}),
        derived(sql_query={"params": {"team 1": "v"}}),
        derived(sql_query={"params": {"team\t1": "v"}}),
        derived(sql_query={"params": {"team 1": "v"}}),
        derived(sql_query={"params": {"team\x7f1": "v"}}),
        derived(sql_query={"params": {"team\x001": "v"}}),
        derived(sql_query={"params": {"labels.team_1": "v" * 501}}),
        derived(sql_query={"params": {"a_1": None}}),
        derived(sql_query={"params": {"a_1": ["x"]}}),
        derived(sql_query={"params": {"a_1": {"x": 1}}}),
        derived(sql_query={"params": {"a_1": float("nan")}}),
        derived(sql_query={"params": {"a_1": float("inf")}}),
        derived(sql_query={"params": {"a_1": "a\x00b"}}),
        derived(sql_query={"params": []}),
        derived(sql_query={"params": [["labels.team_1", "payments"]]}),
        {**derived(), "sql_query": list(derived()["sql_query"].items())},
        derived_of_size(MAX_DERIVED_BYTES + 1),
        # Extra keys, inside.
        derived(newer_key=1),
        derived(sql_query={"newer_key": 1}),
        {"cel_roundtrip": "x"},
        {"sql_query": derived()["sql_query"]},
    ],
    ids=[
        "cel_roundtrip-long",
        "cel_roundtrip-empty",
        "cel_roundtrip-number",
        "cel_roundtrip-nul",
        "sql-long",
        "sql-empty",
        "sql-number",
        "sql-surrogate",
        "params-many",
        "key-long",
        "key-empty",
        "key-space",
        "key-tab",
        "key-no-break-space",
        "key-delete",
        "key-nul",
        "value-long",
        "value-null",
        "value-list",
        "value-object",
        "value-nan",
        "value-infinite",
        "value-nul",
        "params-list",
        "params-pairs",
        "sql_query-pairs",
        "bytes",
        "extra-key",
        "extra-key-of-the-sql",
        "no-sql",
        "no-cel_roundtrip",
    ],
)
def test_what_is_refused(value):
    with pytest.raises(pydantic.ValidationError):
        DerivedInput.parse_obj(value)


def test_the_size_is_counted_as_the_browser_counts_it():
    """JSON.stringify: no spaces; UTF-8: an é is two bytes."""
    text = '{"a":"é","b":[1,true]}'
    assert derived_bytes({"a": "é", "b": [1, True]}) == len(text) + 1


def test_unknown_keys_of_the_request_are_ignored():
    """A newer keep-ui may send more."""
    request = ConfirmRequest.parse_obj({"derived": derived(), "newer_key": 1})
    assert request.derived == derived()
    assert ConfirmRequest.parse_obj({}).derived is None


@pytest.mark.parametrize("value", [[], list(derived().items()), "x", 5])
def test_derived_is_an_object(value):
    """Not a list of pairs, which pydantic would take for one."""
    with pytest.raises(pydantic.ValidationError):
        ConfirmRequest.parse_obj({"derived": value})


def test_the_sql_input_is_what_formatquery_gives():
    assert SqlQueryInput.parse_obj(derived()["sql_query"]).params == {
        "labels.team_1": "payments"
    }


@pytest.mark.parametrize("code", sorted(VALIDATION_MESSAGES))
def test_a_validation_error_on_one_field(code):
    error = validation_error("derived", code)
    assert (error.status, error.code, error.field) == (400, code, "derived")
    assert error.message == VALIDATION_MESSAGES[code]
    assert error.extra == {}


def test_a_validation_error_has_a_known_code():
    with pytest.raises(KeyError):
        validation_error("derived", "too_long")
    with pytest.raises(KeyError):
        AssistantError("too_long")


# --- over HTTP ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def keep(app):
    keep = Keep(FIRING)
    equip(app, gateway=keep)
    return keep


@pytest.fixture()
def conversation(db, keep):
    return add_conversation(db, updated_at=ago(days=3))


@pytest.fixture()
def draft(db, conversation):
    return add_draft(db, conversation, "fp-1", "fp-2")


@pytest.fixture()
def takes_derived(monkeypatch):
    """A kind that needs what the browser computes, as the ones that save a filter
    do. What its plan got is recorded."""
    monkeypatch.setattr(
        alert_status.AlertStatusKind, "confirm_input_model", DerivedInput
    )
    given = []
    plan = alert_status.AlertStatusKind.plan

    def recorded(self, ctx):
        given.append(ctx.confirm_input)
        return plan(self, ctx)

    monkeypatch.setattr(alert_status.AlertStatusKind, "plan", recorded)
    return given


def refused(exchange, field: str, code: str) -> None:
    message = VALIDATION_MESSAGES[code]
    assert exchange.status == 400, exchange.text
    assert exchange.json() == {
        "errors": [{"field": field, "code": code, "message": message}],
        "detail": message,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [None, {}, {"derived": None}, {"newer_key": {"x": 1}}],
    ids=["none", "empty", "null", "newer-key"],
)
async def test_a_kind_that_takes_nothing(app, db, keep, draft, body):
    exchange = await post_confirm(app, draft, body=body).finish()
    assert exchange.status == 200, exchange.text
    assert exchange.json()["state"] == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [derived(), {}, "x", [], list(derived().items())],
    ids=["derived", "empty", "text", "list", "pairs"],
)
async def test_derived_sent_to_a_kind_that_takes_none(app, db, keep, draft, value):
    before = read(db, Draft, draft)
    exchange = await post_confirm(app, draft, body={"derived": value}).finish()
    refused(exchange, "derived", "invalid_value")
    assert read(db, Draft, draft) == before
    assert keep.looks == keep.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "derived", 5, True], ids=str)
async def test_a_body_that_is_not_an_object(app, db, keep, draft, body):
    before = read(db, Draft, draft)
    refused(await post_confirm(app, draft, body=body).finish(), "body", "invalid_value")
    assert read(db, Draft, draft) == before
    assert keep.looks == keep.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [derived(), derived_of_size(MAX_DERIVED_BYTES)],
    ids=["golden", "largest"],
)
async def test_a_kind_that_takes_derived_gets_it(
    app, db, keep, draft, takes_derived, value
):
    exchange = await post_confirm(app, draft, body={"derived": value}).finish()
    assert exchange.status == 200, exchange.text
    assert exchange.json()["state"] == "applied"
    [given] = takes_derived
    assert isinstance(given, DerivedInput)
    assert given.dict() == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (None, "field_required"),
        ({}, "field_required"),
        ({"derived": None}, "field_required"),
        ({"derived": "x"}, "invalid_value"),
        ({"derived": list(derived().items())}, "invalid_value"),
        ({"derived": derived(newer_key=1)}, "invalid_value"),
        ({"derived": derived(sql_query={"sql": "s" * 20001})}, "invalid_value"),
        ({"derived": derived_of_size(MAX_DERIVED_BYTES + 1)}, "invalid_value"),
    ],
    ids=["none", "empty", "null", "text", "pairs", "extra-key", "sql-long", "bytes"],
)
async def test_a_kind_that_takes_derived_without_it(
    app, db, keep, draft, takes_derived, body, code
):
    """Before the claim: the draft stays as it is, nothing is read or sent."""
    before = read(db, Draft, draft)
    exchange = await post_confirm(app, draft, body=body).finish()
    refused(exchange, "derived", code)
    assert read(db, Draft, draft) == before
    assert keep.looks == keep.writes == [] and takes_derived == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "me", [{"email": "else@example.com"}, {"tenant_id": OTHER_TENANT}], ids=str
)
@pytest.mark.parametrize(
    "body",
    [{"derived": "x"}, [], {"derived": derived()}, None],
    ids=["bad", "not-an-object", "derived", "none"],
)
async def test_someone_elses_draft_does_not_exist_for_any_body(
    app, db, keep, draft, takes_derived, me, body
):
    keep.on_json("GET", "/whoami", whoami(**me))
    theirs = await post_confirm(app, draft, body=body).finish()
    missing = await post_confirm(app, uuid.uuid4(), body=body).finish()
    assert (theirs.status, theirs.code) == (404, "draft_not_found")
    assert theirs.json() == missing.json()
    assert read(db, Draft, draft)["state"] == "pending"


@pytest.mark.asyncio
async def test_a_kind_this_version_does_not_know_whatever_the_body(
    app, db, keep, conversation
):
    """It cannot tell what the kind takes."""
    draft = add_draft(db, conversation, kind="from_the_future")
    exchange = await post_confirm(app, draft, body={"derived": "x"}).finish()
    assert (exchange.status, exchange.code) == (409, "draft_unsupported")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code"),
    [("applying", "draft_applying"), ("applied", "draft_not_pending")],
)
async def test_the_body_is_checked_before_the_state(
    app, db, keep, conversation, state, code
):
    """A body this draft can take still finds the draft as it is."""
    draft = add_draft(db, conversation, state=state)
    refused(
        await post_confirm(app, draft, body={"derived": derived()}).finish(),
        "derived",
        "invalid_value",
    )
    exchange = await post_confirm(app, draft, body={}).finish()
    assert (exchange.status, exchange.code) == (409, code)
