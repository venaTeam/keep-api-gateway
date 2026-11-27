"""Kinds and tools read through a facade that has no write: beyond the AST pins of
test_parity, they cannot reach one at run time either."""
import ast
import asyncio
import inspect
import uuid
from pathlib import Path

import pytest

import src.services.assistant
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.reader import ReadGateway
from tests.assistant.fakes import TOKEN, FakeGateway, alert, identity, query_result

PACKAGE = Path(src.services.assistant.__file__).parent
CLIENT = Path(client_module.__file__)
READER = PACKAGE / "gateway" / "reader.py"

REQUEST_ID = uuid.uuid4()
WRITE_NAMES = ("write", "_send", "_request", "_http")

# Every read the facade can make, by (method, path template). A new read joins it.
READS = {
    ("POST", "/alerts/query"),
    ("POST", "/alerts/query/count"),
    ("GET", "/alerts/facets/fields"),
    ("GET", "/alerts/{fingerprint}"),
    ("GET", "/preset"),
    ("GET", "/tags"),
    ("GET", "/rules"),
    ("GET", "/dashboard"),
    ("GET", "/alerts/facets"),
    ("POST", "/cel/validate"),
    ("POST", "/alerts/facets/options"),
}
PUBLIC = {
    "for_request",
    "query_alerts",
    "count_alerts",
    "alert_fields",
    "fetch_alerts_by_fingerprints",
    "search_alerts",
    "count_matching",
    "presets",
    "tags",
    "rules",
    "dashboards",
    "facets",
    "check_cel",
    "facet_options",
}


def reader(fake: FakeGateway | None = None, **options) -> ReadGateway:
    fake = fake or FakeGateway()
    return ReadGateway(fake.client(), identity().caller(REQUEST_ID), **options)


# --- no write, by any name -----------------------------------------------------------


def test_the_facade_offers_no_write():
    facade = reader()
    for name in WRITE_NAMES:
        assert name not in dir(facade)
        with pytest.raises(AttributeError):
            getattr(facade, name)
    assert {name for name in dir(facade) if not name.startswith("_")} == PUBLIC


def test_nothing_can_be_added_or_looked_up_by_name():
    facade = reader()
    with pytest.raises(AttributeError):
        facade.write = lambda *args: None  # type: ignore[attr-defined]
    assert ReadGateway.__slots__ == (
        "_client",
        "_caller",
        "_deadline_seconds",
        "_critical",
    )
    assert not hasattr(facade, "__dict__")
    own = vars(ReadGateway)
    for name in ("__getattr__", "__getattribute__", "__setattr__", "__dir__"):
        assert name not in own, name


def test_every_method_of_the_facade_calls_one_read_of_the_client():
    tree = ast.parse(READER.read_text("utf-8"))
    [facade] = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    called: dict[str, set[str]] = {}
    for function in facade.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called[function.name] = {
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func.value) == "self._client"
        }
    assert called == {
        "__init__": set(),
        "for_request": set(),
        "query_alerts": {"query_alerts"},
        "count_alerts": {"count_alerts"},
        "alert_fields": {"alert_fields"},
        "fetch_alerts_by_fingerprints": {"fetch_alerts_by_fingerprints"},
        "search_alerts": {"search_alerts"},
        "count_matching": {"count_matching"},
        "presets": {"presets"},
        "tags": {"tags"},
        "rules": {"rules"},
        "dashboards": {"dashboards"},
        "facets": {"facets"},
        "check_cel": {"check_cel"},
        "facet_options": {"facet_options"},
    }


# --- the read pin ----------------------------------------------------------------------


def _path_template(node: ast.expr) -> str:
    """'/alerts/query', or f"/alerts/{quote(fingerprint, safe='')}" as
    '/alerts/{fingerprint}'."""
    if isinstance(node, ast.Constant):
        return node.value
    assert isinstance(node, ast.JoinedStr), ast.unparse(node)
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(value.value)
            continue
        inner = value.value
        if isinstance(inner, ast.Call):
            inner = inner.args[0]
        assert isinstance(inner, ast.Name), ast.unparse(value)
        parts.append("{" + inner.id + "}")
    return "".join(parts)


def _client_reads() -> dict[str, set[tuple[str, str]]]:
    """Every method of GatewayClient, with each (method, path template) it can send,
    through the methods it calls."""
    tree = ast.parse(CLIENT.read_text("utf-8"))
    [client] = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GatewayClient"
    ]
    sends: dict[str, set[tuple[str, str]]] = {}
    calls: dict[str, set[str]] = {}
    for function in client.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        sends[function.name] = set()
        calls[function.name] = set()
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func.value) == "self"
            ):
                continue
            calls[function.name].add(node.func.attr)
            method, path = (node.args[2:4] + [None, None])[:2]
            if node.func.attr == "_read" and isinstance(method, ast.Constant):
                sends[function.name].add((method.value, _path_template(path)))

    def reach(name: str, seen: set[str]) -> set[tuple[str, str]]:
        if name in seen or name not in sends:
            return set()
        seen.add(name)
        found = set(sends[name])
        for callee in calls[name]:
            found |= reach(callee, seen)
        return found

    return {name: reach(name, set()) for name in sends}


def test_the_facade_reads_exactly_these_routes():
    """Keyed on (method, path template): a read that goes elsewhere fails here."""
    reads = _client_reads()
    reachable = set().union(
        *(reads[name] for name in PUBLIC if name != "for_request")
    )
    assert reachable == READS
    # Who the caller is stays with the identity check, not with kinds or tools.
    assert ("GET", "/whoami") in reads["whoami"]
    assert "whoami" not in PUBLIC


def test_kinds_and_tools_never_name_the_client():
    for folder in ("kinds", "tools"):
        paths = list((PACKAGE / folder).glob("*.py"))
        assert paths
        for path in paths:
            tree = ast.parse(path.read_text("utf-8"))
            names = {
                part
                for node in ast.walk(tree)
                for field in ("id", "attr", "name", "asname", "module")
                if isinstance(getattr(node, field, None), str)
                for part in getattr(node, field).split(".")
            }
            assert "GatewayClient" not in names, path.name
            assert "_client" not in names, path.name


# --- on whose behalf ---------------------------------------------------------------------


def test_reads_carry_the_callers_token_and_request_id():
    fake = FakeGateway().on_json("POST", "/alerts/query", query_result(alert("fp-1")))
    asyncio.run(reader(fake).query_alerts('name == "x"', 5))
    [request] = fake.requests
    assert request.headers["Authorization"] == TOKEN
    assert request.headers["X-Request-ID"] == str(REQUEST_ID)


def test_another_request_id_for_the_same_user():
    fake = FakeGateway().on_json("POST", "/alerts/query/count", 3)
    draft_id = uuid.uuid4()
    other = reader(fake).for_request(draft_id)
    assert isinstance(other, ReadGateway)
    assert asyncio.run(other.count_alerts('name == "x"')) == 3
    [request] = fake.requests
    assert request.headers["Authorization"] == TOKEN
    assert request.headers["X-Request-ID"] == str(draft_id)


class Recorder:
    """Stands in for the client: what each read was asked."""

    def __init__(self):
        self.asked: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        async def read(*args, **options):
            self.asked.append((name, args, options))

        return read


READ_ORDER = [
    "query_alerts",
    "count_alerts",
    "alert_fields",
    "fetch_alerts_by_fingerprints",
    "search_alerts",
    "count_matching",
    "presets",
    "tags",
    "rules",
    "dashboards",
    "facets",
    "check_cel",
    "facet_options",
]


def every_read(facade: ReadGateway) -> None:
    """Each read of the facade once, in READ_ORDER."""
    assert set(READ_ORDER) == PUBLIC - {"for_request"}
    asyncio.run(facade.query_alerts('name == "x"', 5))
    asyncio.run(facade.count_alerts('name == "x"'))
    asyncio.run(facade.alert_fields())
    asyncio.run(facade.fetch_alerts_by_fingerprints(["fp-1"]))
    asyncio.run(facade.search_alerts('name == "x"', 5, "name", "asc"))
    asyncio.run(facade.count_matching('name == "x"'))
    asyncio.run(facade.presets())
    asyncio.run(facade.tags())
    asyncio.run(facade.rules())
    asyncio.run(facade.dashboards())
    asyncio.run(facade.facets())
    asyncio.run(facade.check_cel('name == "x"', "alerts"))
    asyncio.run(facade.facet_options("", str(uuid.uuid4())))


def test_a_deadline_set_for_the_reader_is_the_default_of_its_reads():
    recorder = Recorder()
    caller = identity().caller(REQUEST_ID)
    bound = ReadGateway(recorder, caller, deadline_seconds=0.5)  # type: ignore[arg-type]
    asyncio.run(bound.fetch_alerts_by_fingerprints(["fp-1"]))
    asyncio.run(bound.fetch_alerts_by_fingerprints(["fp-1"], deadline_seconds=2))
    unbound = ReadGateway(recorder, caller)  # type: ignore[arg-type]
    asyncio.run(unbound.fetch_alerts_by_fingerprints(["fp-1"], known_missing=["x"]))
    assert [(args, options) for _, args, options in recorder.asked] == [
        (
            (caller, ["fp-1"]),
            {"deadline_seconds": 0.5, "known_missing": (), "critical": False},
        ),
        (
            (caller, ["fp-1"]),
            {"deadline_seconds": 2, "known_missing": (), "critical": False},
        ),
        (
            (caller, ["fp-1"]),
            {"deadline_seconds": None, "known_missing": ["x"], "critical": False},
        ),
    ]
    recorder.asked.clear()
    every_read(bound)
    assert [(name, o["deadline_seconds"]) for name, _, o in recorder.asked] == [
        (name, 0.5) for name in READ_ORDER
    ]


def test_a_critical_reader_takes_critical_places_for_every_read():
    """Confirm's and reconcile's reads: no chat may take their places."""
    recorder = Recorder()
    caller = identity().caller(REQUEST_ID)
    every_read(ReadGateway(recorder, caller, critical=True))  # type: ignore[arg-type]
    reads = len(READ_ORDER)
    assert [o["critical"] for _, _, o in recorder.asked] == [True] * reads
    recorder.asked.clear()
    every_read(ReadGateway(recorder, caller))  # type: ignore[arg-type]
    assert [o["critical"] for _, _, o in recorder.asked] == [False] * reads


def test_a_reader_for_another_request_is_critical_only_when_asked():
    recorder = Recorder()
    critical = ReadGateway(
        recorder,  # type: ignore[arg-type]
        identity().caller(REQUEST_ID),
        critical=True,
    )
    every_read(critical.for_request(uuid.uuid4()))
    every_read(critical.for_request(uuid.uuid4(), critical=True))
    reads = len(READ_ORDER)
    assert [o["critical"] for _, _, o in recorder.asked] == [False] * reads + [
        True
    ] * reads


def test_a_reader_for_another_request_does_not_keep_the_deadline():
    recorder = Recorder()
    bound = ReadGateway(
        recorder,  # type: ignore[arg-type]
        identity().caller(REQUEST_ID),
        deadline_seconds=0.5,
    )
    asyncio.run(bound.for_request(uuid.uuid4()).fetch_alerts_by_fingerprints([]))
    asyncio.run(
        bound.for_request(uuid.uuid4(), deadline_seconds=3).fetch_alerts_by_fingerprints(
            []
        )
    )
    assert [options["deadline_seconds"] for _, _, options in recorder.asked] == [None, 3]


def test_a_turns_instant_goes_with_its_reads_and_only_comes_sooner():
    """A chat turn's: every reader made from the turn's, a draft's included, ends its
    calls in time for the answer."""
    assert reader()._caller.deadline_at is None
    turn = reader().for_request(uuid.uuid4(), deadline_at=100.0)
    draft = turn.for_request(uuid.uuid4())
    sooner = turn.for_request(uuid.uuid4(), deadline_at=50.0)
    later = turn.for_request(uuid.uuid4(), deadline_at=200.0)
    assert [r._caller.deadline_at for r in (turn, draft, sooner, later)] == [
        100.0,
        100.0,
        50.0,
        100.0,
    ]
    assert draft._caller.request_id != turn._caller.request_id
    # Each read hands the client the reader's caller, instant and all.
    recorder = Recorder()
    every_read(ReadGateway(recorder, draft._caller))  # type: ignore[arg-type]
    assert [args[0] for _, args, _ in recorder.asked] == [draft._caller] * len(
        READ_ORDER
    )


def test_the_reads_are_the_clients_own():
    """The same signatures, less the caller and whether the call is critical, which
    the reader decides: nothing is added on the way."""
    client = client_module.GatewayClient
    for name in PUBLIC - {"for_request"}:
        theirs = list(inspect.signature(getattr(client, name)).parameters)
        ours = list(inspect.signature(getattr(ReadGateway, name)).parameters)
        assert theirs[:2] == ["self", "caller"]
        assert theirs[-1] == "critical", name
        assert ours == ["self", *theirs[2:-1]], name
