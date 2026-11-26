"""The writes the assistant may send: each one a row of WRITE_ROUTES, checked before a
place is taken. What does not fit its row is never sent."""
import asyncio
import dataclasses
import logging
from types import MappingProxyType

import httpx
import pytest

from src.services.assistant.kinds.alert_status import status_change
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.client import (
    MAX_CHAT_CALLS,
    WRITE_ROUTES,
    WriteAnswer,
    WriteRoute,
)
from src.services.assistant.gateway.errors import GatewayUnavailable
from tests.assistant.fakes import REQUEST_ID, TOKEN, FakeGateway, caller
from tests.assistant.logs import rendered

CANARY = "canary-write-3b9e"
THING = "0b5a3c1e-8f2d-4c6a-9e7b-1d2f3a4b5c6d"


def enrich_body(**changes) -> dict:
    body, _ = status_change(["fp-1", "fp-2"], "acknowledged", "on it", False)
    return {**body, **changes}


def enrichments(**changes) -> dict:
    return enrich_body(enrichments={"status": "acknowledged", **changes})


# A request that fits, per row. A new row needs one here.
VALID = {
    "batch_enrich": {
        "body": enrich_body(),
        "query": {"dispose_on_new_alert": "false"},
    },
}

# Requests outside their row: (row, what is changed in its valid request).
REFUSED = {
    "an extra key": ("batch_enrich", {"body": enrich_body(extra=1)}),
    "a missing key": ("batch_enrich", {"body": {"fingerprints": ["fp-1"]}}),
    "a cel next to the fingerprints": (
        "batch_enrich",
        {"body": enrich_body(cel="true")},
    ),
    "a cel instead of the fingerprints": (
        "batch_enrich",
        {"body": {"cel": "true", "enrichments": {"status": "acknowledged"}}},
    ),
    "21 fingerprints": (
        "batch_enrich",
        {"body": enrich_body(fingerprints=[f"fp-{n}" for n in range(21)])},
    ),
    "no fingerprints": ("batch_enrich", {"body": enrich_body(fingerprints=[])}),
    "a fingerprint that is not a string": (
        "batch_enrich",
        {"body": enrich_body(fingerprints=["fp-1", 2])},
    ),
    "an empty fingerprint": ("batch_enrich", {"body": enrich_body(fingerprints=[""])}),
    "fingerprints that are not a list": (
        "batch_enrich",
        {"body": enrich_body(fingerprints="fp-1")},
    ),
    "an unknown enrichment key": (
        "batch_enrich",
        {"body": enrichments(assignee="someone@example.com")},
    ),
    "an enrichment that is not a string": (
        "batch_enrich",
        {"body": enrichments(note={"text": "x"})},
    ),
    "enrichments that are not an object": (
        "batch_enrich",
        {"body": enrich_body(enrichments=["status"])},
    ),
    "a query value that is not true or false": (
        "batch_enrich",
        {"query": {"dispose_on_new_alert": "yes"}},
    ),
    "a query value that is a boolean": (
        "batch_enrich",
        {"query": {"dispose_on_new_alert": True}},
    ),
    "an unknown query key": (
        "batch_enrich",
        {"query": {"dispose_on_new_alert": "false", "cel": "true"}},
    ),
    "a list body on an object row": (
        "batch_enrich",
        {"body": [enrich_body()]},
    ),
    "a path parameter on a row without one": (
        "batch_enrich",
        {"path_params": {"id": THING}},
    ),
    "a row that does not exist": ("batch_enrich_everything", {}),
}


def request(endpoint: str, **changes) -> dict:
    return {**VALID.get(endpoint, VALID["batch_enrich"]), **changes}


@pytest.fixture()
def gateway():
    return FakeGateway()


def must_not_send(client) -> None:
    """Not even a place is asked for."""

    async def send(*args, **kwargs):
        raise AssertionError("sent")

    client._send = send


def ok_for(endpoint: str) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "id": THING})


def wire_path(endpoint: str) -> str:
    return WRITE_ROUTES[endpoint].path.format_map(
        VALID[endpoint].get("path_params", {})
    )


# --- the table --------------------------------------------------------------------------


def test_every_row_has_a_request_that_fits():
    assert set(VALID) == set(WRITE_ROUTES)


def test_the_table_cannot_be_changed():
    assert isinstance(WRITE_ROUTES, MappingProxyType)
    with pytest.raises(TypeError):
        WRITE_ROUTES["anything"] = WRITE_ROUTES["batch_enrich"]  # type: ignore[index]
    for route in WRITE_ROUTES.values():
        with pytest.raises(dataclasses.FrozenInstanceError):
            route.path = "/alerts/anything"  # type: ignore[misc]
        assert isinstance(route.query, MappingProxyType)
        assert all(isinstance(values, frozenset) for values in route.query.values())
        assert isinstance(route.path_params, tuple)
        assert route.allowed_keys is None or isinstance(route.allowed_keys, frozenset)


# --- what does not fit ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", REFUSED)
async def test_what_does_not_fit_is_never_sent(gateway, caplog, case):
    endpoint, changes = REFUSED[case]
    client = gateway.client()
    must_not_send(client)
    with pytest.raises(GatewayUnavailable) as raised:
        await client.write(caller(), endpoint, **request(endpoint, **changes))
    assert raised.value.sent is False
    assert gateway.requests == []
    assert not client._running and not client._writes
    [line] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert line.getMessage() == "gateway: write outside its route, not sent"
    assert line.endpoint == (endpoint if endpoint in WRITE_ROUTES else None)


@pytest.mark.asyncio
async def test_what_fits_is_sent_as_it_is(gateway):
    gateway.on("POST", "/alerts/batch_enrich", ok_for("batch_enrich"))
    answer = await gateway.client().write(caller(), "batch_enrich", **VALID["batch_enrich"])
    assert answer == WriteAnswer("ok", {"status": "ok", "id": THING})
    [sent] = gateway.requests
    assert sent.url.query == b"dispose_on_new_alert=false"
    assert gateway.body() == VALID["batch_enrich"]["body"]


# --- the machinery, on rows of its own --------------------------------------------------


def checked(body) -> None:
    if body.get("name") == "bad":
        raise ValueError("name")


def short_list(body) -> None:
    if len(body) > 2:
        raise ValueError("length")


def broken(body) -> None:
    body["no such key"]  # a guard with a bug refuses


def classify(response: httpx.Response) -> str:
    return "ok" if response.status_code == 200 else "http_error"


def id_of(body) -> str | None:
    return body.get("id") if isinstance(body, dict) else None


OBJECT_ROW = WriteRoute(
    method="PUT",
    path="/things/{thing_id}",
    path_params=("thing_id",),
    query=MappingProxyType({}),
    allowed_keys=frozenset({"name"}),
    check_body=checked,
    classify=classify,
    object_id=id_of,
    timeout_seconds=1.0,
)
LIST_ROW = dataclasses.replace(
    OBJECT_ROW,
    method="POST",
    path="/things",
    path_params=(),
    allowed_keys=None,
    check_body=short_list,
)
BROKEN_ROW = dataclasses.replace(OBJECT_ROW, check_body=broken)


@pytest.fixture()
def rows(monkeypatch):
    monkeypatch.setattr(
        client_module,
        "WRITE_ROUTES",
        MappingProxyType(
            {"thing": OBJECT_ROW, "things": LIST_ROW, "broken": BROKEN_ROW}
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path_params",
    [
        {},
        {"thing_id": THING, "other_id": THING},
        {"other_id": THING},
        {"thing_id": THING.upper()},
        {"thing_id": "{" + THING + "}"},
        {"thing_id": f"urn:uuid:{THING}"},
        {"thing_id": THING.replace("-", "")},
        {"thing_id": f"{THING}/../../alerts/batch_enrich"},
        {"thing_id": "../alerts"},
        {"thing_id": 1},
    ],
    ids=str,
)
async def test_path_parameters_are_canonical_uuids(gateway, rows, path_params):
    client = gateway.client()
    must_not_send(client)
    with pytest.raises(GatewayUnavailable) as raised:
        await client.write(
            caller(), "thing", body={"name": "x"}, path_params=path_params
        )
    assert raised.value.sent is False
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_a_path_parameter_goes_into_the_path(gateway, rows):
    gateway.on_json("PUT", f"/things/{THING}", {"id": THING})
    answer = await gateway.client().write(
        caller(), "thing", body={"name": "x"}, path_params={"thing_id": THING}
    )
    assert (answer.result, answer.object_id) == ("ok", THING)
    assert gateway.paths() == [f"PUT /things/{THING}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("thing", {"name": "bad"}),
        ("thing", {"name": "x", "id": THING}),
        ("thing", ["name"]),
        ("things", {"name": "x"}),
        ("things", [{}, {}, {}]),
        ("broken", {"name": "x"}),
    ],
    ids=[
        "its-guard",
        "a-key-too-many",
        "a-list",
        "an-object-for-a-list",
        "a-list-its-guard-refuses",
        "a-bug",
    ],
)
async def test_bodies_are_checked_by_their_row(gateway, rows, endpoint, body):
    client = gateway.client()
    must_not_send(client)
    path_params = {"thing_id": THING} if endpoint != "things" else {}
    with pytest.raises(GatewayUnavailable) as raised:
        await client.write(caller(), endpoint, body=body, path_params=path_params)
    assert raised.value.sent is False
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_a_list_row_takes_a_list(gateway, rows):
    gateway.on_json("POST", "/things", [{"id": THING}])
    answer = await gateway.client().write(caller(), "things", body=[{"name": "x"}])
    assert answer == WriteAnswer("ok", [{"id": THING}], None)
    assert gateway.body() == [{"name": "x"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (httpx.Response(200, json={"id": THING}), WriteAnswer("ok", {"id": THING}, THING)),
        (
            httpx.Response(201, json={"id": THING}),
            WriteAnswer("http_error", {"id": THING}, None),
        ),
        (
            httpx.Response(
                400, json={"detail": {"code": "INVALID_CEL", "message": CANARY}}
            ),
            WriteAnswer("http_error", None, None, invalid_cel=True),
        ),
        (
            httpx.Response(400, json={"detail": {"code": "OTHER", "message": CANARY}}),
            WriteAnswer("http_error", None, None),
        ),
        (httpx.Response(500, text=CANARY), WriteAnswer("http_error")),
    ],
    ids=["ok", "2xx-not-ok", "invalid-cel", "other-400", "500"],
)
async def test_what_the_answer_carries(gateway, rows, answer, expected):
    gateway.on("PUT", f"/things/{THING}", answer)
    assert (
        await gateway.client().write(
            caller(), "thing", body={"name": "x"}, path_params={"thing_id": THING}
        )
        == expected
    )


@pytest.mark.asyncio
async def test_the_rows_own_timeout(rows):
    async def hang(request):
        await asyncio.Event().wait()

    client = FakeGateway().on("PUT", f"/things/{THING}", hang).client()
    started = asyncio.get_running_loop().time()
    answer = await client.write(
        caller(), "thing", body={"name": "x"}, path_params={"thing_id": THING}
    )
    assert answer == WriteAnswer("unknown")
    # 1 s, and the late-answer wait.
    assert 0.9 < asyncio.get_running_loop().time() - started < 2
    # Can be made shorter, not longer.
    started = asyncio.get_running_loop().time()
    await client.write(
        caller(),
        "thing",
        body={"name": "x"},
        path_params={"thing_id": THING},
        timeout=30,
    )
    assert asyncio.get_running_loop().time() - started < 2


# --- per row ----------------------------------------------------------------------------


class Held:
    """Reads never end until `release`; writes answer at once, or are held too."""

    def __init__(self, hold_writes: bool = False):
        self.release = asyncio.Event()
        self.hold_writes = hold_writes
        self.fake = FakeGateway(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        is_write = request.url.path not in ("/alerts/query", "/whoami")
        if not is_write or self.hold_writes:
            await self.release.wait()
        return httpx.Response(200, json={"status": "ok", "id": THING})


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", WRITE_ROUTES)
async def test_every_row_takes_the_reserved_place(endpoint):
    held = Held()
    client = held.fake.client()
    # Every other place: the chats' and the critical one.
    for critical in [False] * MAX_CHAT_CALLS + [True]:
        with pytest.raises(GatewayUnavailable) as raised:
            await client._send(
                caller(),
                "query",
                "POST",
                "/alerts/query",
                timeout=0.01,
                body={},
                critical=critical,
            )
        assert raised.value.sent is True
    with pytest.raises(GatewayUnavailable) as raised:
        await client.whoami(caller())
    assert raised.value.sent is False

    answer = await client.write(caller(), endpoint, **VALID[endpoint])
    assert answer.result == "ok"
    held.release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", WRITE_ROUTES)
async def test_every_row_is_waited_for_at_shutdown(endpoint):
    held = Held(hold_writes=True)
    client = held.fake.client()
    answer = await client.write(caller(), endpoint, timeout=0.01, **VALID[endpoint])
    assert answer == WriteAnswer("unknown")
    assert await client.wait_for_writes(0.01) == 1
    held.release.set()
    assert await client.wait_for_writes(2) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", WRITE_ROUTES)
async def test_every_row_says_where_it_comes_from(gateway, endpoint):
    gateway.on(WRITE_ROUTES[endpoint].method, wire_path(endpoint), ok_for(endpoint))
    await gateway.client().write(caller(), endpoint, **VALID[endpoint])
    [sent] = gateway.requests
    assert sent.headers["X-Keep-Source"] == "assistant"
    assert sent.headers["Authorization"] == TOKEN
    assert sent.headers["X-Request-ID"] == REQUEST_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", WRITE_ROUTES)
@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 500, 503])
async def test_an_error_body_stays_where_it_is(gateway, caplog, endpoint, status):
    caplog.set_level(logging.DEBUG)
    gateway.on_json(
        WRITE_ROUTES[endpoint].method,
        wire_path(endpoint),
        {"detail": CANARY, "id": THING},
        status=status,
    )
    answer = await gateway.client().write(caller(), endpoint, **VALID[endpoint])
    assert answer.body is None and answer.object_id is None
    assert answer.result != "ok"
    assert CANARY not in repr(answer)
    assert not [r for r in caplog.records if CANARY in rendered(r)]
