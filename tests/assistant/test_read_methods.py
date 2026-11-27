"""The reads of the tenant's lists and checks: their classes, the answer cap, what the
reader strips, and the turn's memo."""
import asyncio
import dataclasses
import json
import threading
import time
import uuid

import httpx
import pytest

from src.services.assistant.core import metrics
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.errors import (
    AnswerTooLarge,
    GatewayUnavailable,
    InvalidCel,
    RulesUnreadable,
)
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.gateway.views import CarriedRuleFields
from src.services.assistant.tools.base import (
    FilterBudgetSpent,
    ToolContext,
    read_failure_outcome,
)
from tests.assistant.fakes import FakeGateway, alert, identity, query_result

OWN = "ops@example.com"
OTHER = "someone.else@example.com"
FACET_ID = "f8a91ac7-4916-4ad0-9b46-a5ddb85bfbb8"


def preset(name: str, created_by: str | None = OTHER, **fields) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "options": [{"label": "CEL", "value": 'severity == "critical"'}],
        "created_by": created_by,
        "is_private": False,
        "is_noisy": False,
        "counter_shows_firing_only": False,
        "should_do_noise_now": False,
        "static": False,
        "tags": [{"id": 1, "name": "db"}],
        **fields,
    }


def rule(name: str, created_by: str = OTHER, **fields) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": "keep",
        "name": name,
        "definition_cel": 'service == "db"',
        "definition_cel_ast": {"node": "tree"},
        "timeframe": 600,
        "timeunit": "seconds",
        "created_by": created_by,
        "updated_by": OTHER,
        "assignee": "oncall@example.com",
        "group_description": "the description",
        "multi_level": True,
        "multi_level_property_name": "labels.host",
        "grouping_criteria": ["labels.host"],
        "distribution": {"2026-11-24": 3},
        "incidents": 2,
        **fields,
    }


def dashboard() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "dashboard_name": "Ops",
        "dashboard_config": {
            "layout": [],
            "widgetData": [
                {"name": "Critical", "preset": preset("critical", created_by=OWN)}
            ],
        },
        "created_at": "2026-11-24T09:58:00",
        "updated_at": "2026-11-24T09:58:00",
    }


def reader_of(fake: FakeGateway, **identity_fields) -> ReadGateway:
    return fake.reader(identity(**identity_fields))


def run(coroutine):
    return asyncio.run(coroutine)


def emails_in(value) -> list[str]:
    text = json.dumps(value)
    return [email for email in (OWN, OTHER, "oncall@example.com") if email in text]


# --- what the reader strips --------------------------------------------------------


def test_presets_lose_their_people_and_say_whose_they_are():
    fake = FakeGateway().on_json(
        "GET",
        "/preset",
        [
            preset("mine", created_by=OWN, tenant_id="keep", updated_by=OWN),
            preset("theirs"),
            preset("feed", created_by=None, static=True),
        ],
    )
    rows = run(reader_of(fake).presets())
    assert [(row["name"], row["mine"]) for row in rows] == [
        ("mine", True),
        ("theirs", False),
        ("feed", False),
    ]
    assert emails_in(rows) == []
    assert not {"created_by", "updated_by", "tenant_id"} & set().union(*rows)
    # What the reader does not know about stays: the model may need it.
    assert rows[0]["tags"] == [{"id": 1, "name": "db"}]


def test_without_an_email_nothing_is_mine():
    fake = FakeGateway().on_json("GET", "/preset", [preset("x", created_by="")])
    [row] = run(reader_of(fake, email="").presets())
    assert row["mine"] is False


def test_a_dashboards_embedded_presets_lose_their_people():
    fake = FakeGateway().on_json("GET", "/dashboard", [dashboard()])
    [row] = run(reader_of(fake).dashboards())
    assert emails_in(row) == []
    embedded = row["dashboard_config"]["widgetData"][0]["preset"]
    assert embedded["name"] == "critical" and "created_by" not in embedded


def test_tags_and_facets_lose_the_tenant():
    fake = (
        FakeGateway()
        .on_json("GET", "/tags", [{"id": 1, "tenant_id": "keep", "name": "db"}])
        .on_json(
            "GET",
            "/alerts/facets",
            [{"id": FACET_ID, "property_path": "severity", "name": "Severity"}],
        )
    )
    reader = reader_of(fake)
    assert run(reader.tags()) == [{"id": 1, "name": "db"}]
    assert run(reader.facets()) == [
        {"id": FACET_ID, "property_path": "severity", "name": "Severity"}
    ]


def test_a_rule_shows_no_people_and_carries_what_an_update_needs():
    fake = FakeGateway().on_json("GET", "/rules", [rule("mine", created_by=OWN)])
    [read] = run(reader_of(fake).rules())
    assert read.fields["mine"] is True
    assert emails_in(read.fields) == []
    hidden = {
        "created_by",
        "updated_by",
        "tenant_id",
        "assignee",
        "group_description",
        "multi_level",
        "multi_level_property_name",
        "distribution",
        "definition_cel_ast",
    }
    assert not hidden & read.fields.keys()
    assert read.fields["grouping_criteria"] == ["labels.host"]
    assert read.carried == CarriedRuleFields(
        assignee="oncall@example.com",
        group_description="the description",
        multi_level=True,
        multi_level_property_name="labels.host",
    )
    # Never encoded, never shown by a repr.
    with pytest.raises(TypeError):
        json.dumps(read.carried)
    for text in (repr(read), repr(read.carried)):
        assert "oncall@example.com" not in text and "the description" not in text
    with pytest.raises(dataclasses.FrozenInstanceError):
        read.carried.assignee = "x"  # type: ignore[misc]


def test_the_callers_email_is_never_sent_nor_shown():
    caller = identity().caller(uuid.uuid4())
    assert caller.email == OWN and OWN not in repr(caller)
    fake = FakeGateway().on_json("GET", "/preset", [])
    run(reader_of(fake).presets())
    [request] = fake.requests
    assert OWN not in str(request.headers) and OWN not in request.content.decode()


# --- the classes, and what each read sends ---------------------------------------


READS = [
    # (name, arguments, endpoint, class, answer)
    ("presets", (), "presets", "lookup", []),
    ("tags", (), "tags", "lookup", []),
    ("rules", (), "rules", "filter", []),
    ("dashboards", (), "dashboards", "lookup", []),
    ("facets", (), "facets", "lookup", []),
    ("check_cel", ("true", "alerts"), "cel_validate", "lookup", {"valid": True}),
    ("facet_options", ("", FACET_ID), "facet_options", "filter", {FACET_ID: []}),
    ("search_alerts", ("true", 5, "name", "asc"), "query", "filter", query_result()),
    ("count_matching", ("true",), "count", "filter", 0),
]


@pytest.mark.parametrize(("name", "args", "endpoint", "call_class", "answer"), READS)
def test_each_read_has_its_class(monkeypatch, name, args, endpoint, call_class, answer):
    client = FakeGateway().client()
    asked = []

    async def read(caller, endpoint, method, path, **options):
        asked.append((endpoint, options["call_class"], options["critical"]))
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(client, "_read", read)
    reader = ReadGateway(client, identity().caller(uuid.uuid4()))
    run(getattr(reader, name)(*args))
    assert asked == [(endpoint, call_class, False)]


def test_the_classes_are_the_ones_the_load_table_names():
    assert dict(client_module.CALL_CLASSES) == {
        "filter": client_module.CallClass(limit=4, waits=True),
        "lookup": client_module.CallClass(limit=2, waits=True),
        "alert": client_module.CallClass(limit=4, waits=True),
    }


def test_what_the_checks_and_the_search_send():
    fake = (
        FakeGateway()
        .on_json("POST", "/cel/validate", {"valid": False, "diagnostics": []})
        .on_json("POST", "/alerts/facets/options", {FACET_ID: [{"value": "high"}]})
        .on_json("POST", "/alerts/query", query_result(alert("fp-1")))
    )
    reader = reader_of(fake)
    assert run(reader.check_cel("a < 5", "rules")) == {
        "valid": False,
        "diagnostics": [],
    }
    assert run(reader.facet_options('name == "x"', FACET_ID)) == [{"value": "high"}]
    [found] = run(reader.search_alerts("true", 3, "severity", "desc"))
    assert found.fingerprint == "fp-1"
    assert [json.loads(r.content) for r in fake.requests] == [
        {"cel": "a < 5", "context": "rules"},
        {"cel": 'name == "x"', "facet_queries": {FACET_ID: ""}},
        {
            "cel": "true",
            "limit": 3,
            "sort_options": [{"sort_by": "severity_order", "sort_dir": "desc"}],
        },
    ]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("facet_options", ("", "severity")),
        ("facet_options", ("", FACET_ID.upper())),
        ("check_cel", ("true", "workflows")),
        ("search_alerts", ("true", 5, "fingerprint", "asc")),
        ("search_alerts", ("true", 5, "name", "up")),
    ],
)
def test_what_the_gateway_would_answer_with_a_500_is_never_sent(name, args):
    fake = FakeGateway()
    with pytest.raises(ValueError):
        run(getattr(reader_of(fake), name)(*args))
    assert fake.requests == []


def test_a_count_that_fails_is_raised_by_count_matching_only():
    invalid = {"detail": {"code": "INVALID_CEL", "diagnostics": [{"message": "bad"}]}}
    fake = FakeGateway().on_json("POST", "/alerts/query/count", invalid, status=400)
    reader = reader_of(fake)
    assert run(reader.count_alerts("bad(")) is None
    with pytest.raises(InvalidCel):
        run(reader.count_matching("bad("))


# --- failures ----------------------------------------------------------------------


def test_an_unreadable_rule_is_its_own_failure():
    fake = (
        FakeGateway()
        .on_json("GET", "/rules", {"message": "x"}, status=500)
        .on_json("GET", "/preset", {"message": "x"}, status=500)
    )
    reader = reader_of(fake)
    with pytest.raises(RulesUnreadable):
        run(reader.rules())
    with pytest.raises(GatewayUnavailable) as other:
        run(reader.presets())
    assert not isinstance(other.value, RulesUnreadable)
    assert other.value.reason == "status_500"


def test_the_tool_errors_of_the_new_failures():
    assert read_failure_outcome(RulesUnreadable()).result == {
        "status": "error",
        "code": "rules_unreadable",
    }
    assert read_failure_outcome(RulesUnreadable()).stream_error is None
    too_large = read_failure_outcome(AnswerTooLarge())
    assert too_large.result == {"status": "error", "code": "too_large"}
    assert too_large.stream_error is None


@pytest.mark.parametrize("name", ["presets", "tags", "dashboards", "facets"])
def test_a_list_that_is_not_a_list_of_objects_is_unusable(name):
    path = {
        "presets": "/preset",
        "tags": "/tags",
        "dashboards": "/dashboard",
        "facets": "/alerts/facets",
    }[name]
    fake = FakeGateway().on_json("GET", path, ["a"])
    with pytest.raises(GatewayUnavailable):
        run(getattr(reader_of(fake), name)())


@pytest.mark.parametrize(("name", "args", "endpoint", "call_class", "answer"), READS)
def test_a_turns_instant_bounds_each_read(name, args, endpoint, call_class, answer):
    fake = FakeGateway()
    reader = fake.reader().for_request(uuid.uuid4(), deadline_at=time.monotonic() - 1)
    with pytest.raises(GatewayUnavailable) as failed:
        run(getattr(reader, name)(*args))
    assert failed.value.reason == "not_sent"
    assert fake.requests == []


# --- the answer cap ----------------------------------------------------------------


def series(endpoint: str, status_class: str) -> float:
    return metrics.gateway_requests_total.labels(
        endpoint=endpoint, status_class=status_class
    )._value.get()


def test_the_caps():
    assert client_module.MAX_READ_BYTES == 2 * 1024 * 1024
    assert client_module.PARSE_IN_THREAD_BYTES == 256 * 1024
    assert "too_large" in metrics.STATUS_CLASSES


def test_an_answer_above_the_cap_is_not_read(monkeypatch):
    rows = [preset("p")]
    size = len(httpx.Response(200, json=rows).content)
    fake = FakeGateway().on_json("GET", "/preset", rows)
    monkeypatch.setattr(client_module, "MAX_READ_BYTES", size)
    reader = reader_of(fake)
    assert len(run(reader.presets())) == 1  # at the cap
    monkeypatch.setattr(client_module, "MAX_READ_BYTES", size - 1)
    before = series("presets", "too_large")
    with pytest.raises(AnswerTooLarge) as failed:
        run(reader.presets())
    assert failed.value.reason == "too_large" and failed.value.sent
    assert series("presets", "too_large") == before + 1


def test_a_large_answer_is_parsed_in_a_thread(monkeypatch):
    threads = []
    parse = client_module._json

    def spy(response):
        threads.append(threading.get_ident())
        return parse(response)

    monkeypatch.setattr(client_module, "_json", spy)
    small = [preset("p")]
    large = [preset("p", description="x" * 600)]
    limit = len(httpx.Response(200, json=small).content)
    monkeypatch.setattr(client_module, "PARSE_IN_THREAD_BYTES", limit)
    fake = FakeGateway().on_json("GET", "/preset", small).on_json("GET", "/tags", large)
    reader = reader_of(fake)

    async def both():
        await reader.presets()
        await reader.tags()
        return threading.get_ident()

    loop_thread = run(both())
    assert threads[0] == loop_thread
    assert threads[1] != loop_thread


# --- the turn's memo ---------------------------------------------------------------


def context(fake: FakeGateway) -> ToolContext:
    return ToolContext(identity=identity(), gateway=fake.reader())


def test_a_read_is_made_once_per_turn_and_charged_once():
    fake = FakeGateway().on_json("GET", "/rules", [rule("r")])
    turn = context(fake)
    first = run(turn.read_once(turn.gateway.rules, filter_units=1))
    again = run(turn.read_once(turn.gateway.rules, filter_units=1))
    assert again is first
    assert fake.paths() == ["GET /rules"]
    assert turn.cel_lookups_left == 5


def test_other_arguments_are_another_read():
    fake = FakeGateway().on_json("POST", "/cel/validate", {"valid": True})
    turn = context(fake)
    run(turn.read_once(turn.gateway.check_cel, "a", "alerts"))
    run(turn.read_once(turn.gateway.check_cel, "a", "rules"))
    run(turn.read_once(turn.gateway.check_cel, "a", "alerts"))
    assert len(fake.requests) == 2
    # No filter units: none charged.
    assert turn.cel_lookups_left == 6


def test_a_failed_read_is_not_remembered():
    answers = [httpx.Response(500, json={}), httpx.Response(200, json=[rule("r")])]

    async def handler(request):
        return answers.pop(0)

    fake = FakeGateway(handler)
    turn = context(fake)
    with pytest.raises(RulesUnreadable):
        run(turn.read_once(turn.gateway.rules, filter_units=1))
    assert len(run(turn.read_once(turn.gateway.rules, filter_units=1))) == 1
    # Each one was sent, and each one charged.
    assert len(fake.requests) == 2
    assert turn.cel_lookups_left == 4


def test_without_units_left_nothing_is_sent():
    fake = FakeGateway().on_json("GET", "/rules", [])
    turn = context(fake)
    turn.cel_lookups_left = 0
    with pytest.raises(FilterBudgetSpent):
        run(turn.read_once(turn.gateway.rules, filter_units=1))
    assert fake.requests == []
    assert turn.cel_lookups_left == 0


def test_a_remembered_read_costs_nothing_even_without_units_left():
    fake = FakeGateway().on_json("GET", "/rules", [])
    turn = context(fake)
    run(turn.read_once(turn.gateway.rules, filter_units=6))
    assert turn.cel_lookups_left == 0
    assert run(turn.read_once(turn.gateway.rules, filter_units=1)) == []
    assert len(fake.requests) == 1
