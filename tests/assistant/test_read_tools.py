"""The alert read tools: what they send, what they answer, and what they cost."""
import asyncio
import json
from typing import Any

import pytest

from src.services.assistant.bl import context as context_bl
from src.services.assistant.tools import (
    count_alerts,
    get_alerts,
    get_field_values,
    search_alerts,
    validate_cel,
)
from src.services.assistant.tools.base import ToolContext, encode_result
from src.services.assistant.tools.registry import TOOLS
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.fakes import FakeGateway, alert, identity, query_result
from tests.assistant.test_gateway_samples import READ_ONLY, as_response

READ_TOOLS = {
    "get_field_values": (get_field_values.SPEC, "Reading field values"),
    "validate_cel": (validate_cel.SPEC, "Checking a filter"),
    "count_alerts": (count_alerts.SPEC, "Counting alerts"),
    "search_alerts": (search_alerts.SPEC, "Searching alerts"),
    "get_alerts": (get_alerts.SPEC, "Reading alerts"),
}
ARGUMENT_MODELS = {
    "get_field_values": get_field_values.GetFieldValuesArgs,
    "validate_cel": validate_cel.ValidateCelArgs,
    "count_alerts": count_alerts.CelArgs,
    "search_alerts": search_alerts.SearchAlertsArgs,
    "get_alerts": get_alerts.GetAlertsArgs,
}
SEVERITY_FACET = "f8a91ac7-4916-4ad0-9b46-a5ddb85bfbb8"
QUERY = ("POST", "/alerts/query")
COUNT = ("POST", "/alerts/query/count")
FIELDS = ("GET", "/alerts/facets/fields")
FACETS = ("GET", "/alerts/facets")
OPTIONS = ("POST", "/alerts/facets/options")
CHECK = ("POST", "/cel/validate")
INVALID = {
    "detail": {
        "code": "INVALID_CEL",
        "diagnostics": [{"message": "Error parsing CEL expression: count < "}],
    }
}


def gateway() -> FakeGateway:
    return (
        FakeGateway()
        .on_json(*FIELDS, ["name", "severity", "service", "labels.team", "assignee"])
        .on_json(
            *FACETS,
            [
                {"id": SEVERITY_FACET, "property_path": "severity", "name": "Severity"},
                {"id": "not-a-uuid", "property_path": "service", "name": "Service"},
            ],
        )
    )


def turn(fake: FakeGateway, **identity_fields) -> ToolContext:
    return ToolContext(
        identity=identity(**identity_fields),
        gateway=fake.reader(identity(**identity_fields)),
    )


def run(tool, tool_context: ToolContext, /, **arguments):
    # Positional: validate_cel takes an argument named `context`.
    return asyncio.run(tool.SPEC.run(tool_context, json.dumps(arguments)))


def sent(fake: FakeGateway, method: str, path: str) -> list[dict]:
    return [
        json.loads(r.content) if r.content else None
        for r in fake.requests
        if (r.method, r.url.raw_path.decode()) == (method, path)
    ]


# --- the registry and the schemas ------------------------------------------------------


def test_the_registry_and_what_each_read_tool_is():
    assert list(TOOLS) == [
        "change_alert_status",
        *READ_TOOLS,
        "list_presets",
        "list_correlation_rules",
        "list_dashboards",
        "list_tags",
        "open_page",
    ]
    for name, (spec, label) in READ_TOOLS.items():
        assert TOOLS[name] is spec
        assert (spec.name, spec.label) == (name, label)
        assert spec.draft_kind is None and spec.makes_link is False, name
        assert spec.prompt_guidance.startswith(f"{name}:"), name


UNSUPPORTED_IN_STRICT_MODE = {
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
    "format",
    "default",
}


def _objects(schema: Any):
    """Every object schema inside `schema`."""
    if isinstance(schema, dict):
        assert not UNSUPPORTED_IN_STRICT_MODE & schema.keys(), schema
        if schema.get("type") in ("object", ["object", "null"]):
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


@pytest.mark.parametrize("name", list(TOOLS))
def test_every_schema_is_strict(name):
    """OpenAI's strict mode: every object closed, every property required, an
    optional one nullable."""
    objects = list(_objects(TOOLS[name].parameters))
    assert objects
    for schema in objects:
        assert schema["additionalProperties"] is False, name
        assert sorted(schema["required"]) == sorted(schema["properties"]), name


@pytest.mark.parametrize("name", list(READ_TOOLS))
def test_each_schema_names_the_validated_arguments(name):
    properties = TOOLS[name].parameters["properties"]
    assert sorted(properties) == sorted(ARGUMENT_MODELS[name].__fields__)


def test_what_may_be_null():
    def nullable(tool, argument):
        schema = tool.SPEC.parameters["properties"][argument]
        types = schema.get("type") or [o.get("type") for o in schema["anyOf"]]
        return "null" in types

    assert nullable(get_field_values, "cel")
    assert nullable(search_alerts, "sort")
    assert not nullable(search_alerts, "cel")
    assert not nullable(get_alerts, "fingerprints")


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (count_alerts, {}),
        (count_alerts, {"cel": "a", "extra": 1}),
        (count_alerts, {"cel": "x" * 2001}),
        (search_alerts, {"cel": "", "limit": 0, "sort": None}),
        (search_alerts, {"cel": "", "limit": 21, "sort": None}),
        (search_alerts, {"cel": "", "limit": "5", "sort": None}),
        (search_alerts, {"cel": "", "limit": 5, "sort": {"field": "id", "dir": "asc"}}),
        (get_alerts, {"fingerprints": []}),
        (get_alerts, {"fingerprints": [f"fp-{n}" for n in range(21)]}),
        (get_alerts, {"fingerprints": ["a\nb"]}),
        (validate_cel, {"cel": "", "context": "alerts"}),
        (validate_cel, {"cel": "a", "context": "workflows"}),
        (get_field_values, {"field": "", "cel": None}),
    ],
)
def test_arguments_outside_the_schema_are_refused_before_any_read(tool, arguments):
    fake = gateway()
    outcome = run(tool, turn(fake), **arguments)
    assert outcome.code == "invalid_arguments"
    assert outcome.result["errors"]
    assert fake.requests == []


# --- count_alerts ---------------------------------------------------------------------


def test_a_count():
    fake = gateway().on_json(*COUNT, 7)
    context = turn(fake)
    outcome = run(count_alerts, context, cel='severity == "critical"')
    assert (outcome.code, outcome.result) == ("found", {"status": "found", "count": 7})
    assert context.cel_lookups_left == 5
    # The same filter again in the turn: not sent, not charged.
    run(count_alerts, context, cel='severity == "critical"')
    assert sent(fake, *COUNT) == [{"cel": 'severity == "critical"'}]
    assert context.cel_lookups_left == 5


def test_a_count_the_gateway_cannot_give_is_null():
    fake = gateway().on_json(*COUNT, "many")
    assert run(count_alerts, turn(fake), cel="").result == {
        "status": "found",
        "count": None,
    }


def test_an_invalid_filter_is_told_as_it_was_written():
    fake = gateway().on_json(*COUNT, INVALID, status=400)
    outcome = run(count_alerts, turn(fake), cel="count < ")
    assert outcome.code == "invalid_cel"
    assert outcome.result == {
        "status": "invalid_cel",
        "diagnostics": ["Error parsing CEL expression: count < "],
    }


def test_without_budget_nothing_is_counted():
    fake = gateway().on_json(*COUNT, 7)
    context = turn(fake)
    context.cel_lookups_left = 0
    outcome = run(count_alerts, context, cel="")
    assert outcome.result == {"status": "error", "code": "cel_lookup_limit"}
    assert fake.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthenticated"), (403, "error"), (500, "error"), (502, "error")],
)
def test_a_failed_count(status, code):
    fake = gateway().on_json(*COUNT, {"detail": "no"}, status=status)
    assert run(count_alerts, turn(fake), cel="").code == code


# --- search_alerts --------------------------------------------------------------------


def test_a_search_in_the_order_asked_for():
    alerts = [alert(f"fp-{n}", assignee="oncall@example.com") for n in range(4)]
    fake = gateway().on_json(*QUERY, query_result(*alerts))
    context = turn(fake)
    outcome = run(
        search_alerts,
        context,
        cel="status == 'firing'",
        limit=3,
        sort={"field": "severity", "dir": "asc"},
    )
    assert sent(fake, *QUERY) == [
        {
            "cel": "status == 'firing'",
            "limit": 4,
            "sort_options": [{"sort_by": "severity_order", "sort_dir": "asc"}],
        }
    ]
    assert outcome.code == "found"
    result = outcome.result
    assert (result["count"], result["more"]) == (3, True)
    assert [a["fingerprint"] for a in result["alerts"]] == ["fp-0", "fp-1", "fp-2"]
    assert "oncall@example.com" not in encode_result(result)
    assert context.cel_lookups_left == 5


def test_without_a_sort_the_newest_come_first():
    fake = gateway().on_json(*QUERY, query_result(alert("fp-1")))
    outcome = run(search_alerts, turn(fake), cel="", limit=5, sort=None)
    [body] = sent(fake, *QUERY)
    assert body["sort_options"] == [{"sort_by": "lastReceived", "sort_dir": "desc"}]
    assert outcome.result["more"] is False


def test_a_search_without_matches():
    fake = gateway().on_json(*QUERY, query_result())
    outcome = run(search_alerts, turn(fake), cel="name == 'x'", limit=5, sort=None)
    assert (outcome.code, outcome.result) == (
        "no_matches",
        {"status": "no_matches", "count": 0},
    )


# --- get_alerts -----------------------------------------------------------------------


def test_alerts_by_fingerprint_cost_no_filter():
    fake = gateway().on_json(*QUERY, query_result(alert("fp-1")))
    context = turn(fake)
    outcome = run(get_alerts, context, fingerprints=["fp-1", "fp-gone", "fp-1"])
    assert outcome.code == "found"
    assert [a["fingerprint"] for a in outcome.result["alerts"]] == ["fp-1"]
    assert outcome.result["not_found"] == ["fp-gone"]
    assert outcome.result["unresolvable"] == []
    assert context.cel_lookups_left == 6


def test_alerts_that_are_all_gone():
    fake = gateway().on_json(*QUERY, query_result())
    outcome = run(get_alerts, turn(fake), fingerprints=["fp-gone"])
    assert outcome.code == "not_found"
    assert outcome.result["alerts"] == []


# --- validate_cel ---------------------------------------------------------------------


def test_a_valid_filter_costs_nothing():
    fake = gateway().on_json(*CHECK, {"valid": True, "diagnostics": []})
    context = turn(fake)
    outcome = run(validate_cel, context, cel="count < 5", context="rules")
    assert (outcome.code, outcome.result) == (
        "found",
        {"status": "found", "valid": True, "diagnostics": []},
    )
    assert sent(fake, *CHECK) == [{"cel": "count < 5", "context": "rules"}]
    assert context.cel_lookups_left == 6


def test_an_invalid_filter_keeps_its_diagnostics_readable():
    diagnostics = [{"message": f"at {n}: a < b\nthere"} for n in range(7)]
    fake = gateway().on_json(*CHECK, {"valid": False, "diagnostics": diagnostics})
    outcome = run(validate_cel, turn(fake), cel="a < ", context="alerts")
    assert outcome.code == "invalid_cel"
    assert outcome.result["valid"] is False
    assert outcome.result["diagnostics"] == [f"at {n}: a < b there" for n in range(5)]
    # Readable for the model too: escaped, not replaced.
    assert json.loads(encode_result(outcome.result)) == outcome.result


# --- get_field_values -----------------------------------------------------------------


def options(*pairs) -> dict:
    return {
        SEVERITY_FACET: [
            {"display_name": value, "value": value, "matches_count": count}
            for value, count in pairs
        ]
    }


def test_a_field_with_a_facet_is_counted_by_the_gateway():
    fake = gateway().on_json(
        *OPTIONS, options(("critical", 2), ("high", 0), ("warning", 5))
    )
    context = turn(fake)
    outcome = run(get_field_values, context, field="severity", cel=None)
    assert outcome.code == "found"
    assert outcome.result == {
        "status": "found",
        "field": "severity",
        "values": [
            {"value": "warning", "count": 5},
            {"value": "critical", "count": 2},
        ],
        "sampled": False,
    }
    assert sent(fake, *OPTIONS) == [{"cel": "", "facet_queries": {SEVERITY_FACET: ""}}]
    assert context.cel_lookups_left == 5


def test_any_other_field_is_counted_over_the_newest_matches():
    alerts = [
        alert("fp-1", labels={"team": "db"}),
        alert("fp-2", labels={"team": "db"}),
        alert("fp-3", labels={"team": ["web", "db"]}),
        alert("fp-4", labels={}),
        alert("fp-5", labels={"team": True}),
    ]
    fake = gateway().on_json(*QUERY, query_result(*alerts))
    context = turn(fake)
    outcome = run(get_field_values, context, field="labels.team", cel="name != ''")
    assert sent(fake, *QUERY) == [{"cel": "name != ''", "limit": 200}]
    assert outcome.result == {
        "status": "found",
        "field": "labels.team",
        "values": [
            {"value": "db", "count": 3},
            {"value": "web", "count": 1},
            {"value": "true", "count": 1},
        ],
        "sampled": False,
        "sample_size": 5,
    }
    assert context.cel_lookups_left == 5


def test_a_facet_whose_id_the_gateway_cannot_take_is_counted_over_matches():
    fake = gateway().on_json(*QUERY, query_result(alert("fp-1", service="orders")))
    outcome = run(get_field_values, turn(fake), field="service", cel=None)
    assert sent(fake, *OPTIONS) == []
    assert outcome.result["values"] == [{"value": "orders", "count": 1}]


def test_a_full_sample_says_so(monkeypatch):
    monkeypatch.setattr(get_field_values, "SAMPLE_SIZE", 2)
    fake = gateway().on_json(*QUERY, query_result(alert("a"), alert("b")))
    outcome = run(get_field_values, turn(fake), field="name", cel=None)
    assert (outcome.result["sampled"], outcome.result["sample_size"]) == (True, 2)


@pytest.mark.parametrize(
    "field",
    [
        "assignee",
        "note",
        "Note",
        "created_by",
        "labels.updated_by",
        "enrichments.updatedBy",
        "owner_email",
        "labels.Email",
        "contact.e-mail-EMAIL",
    ],
)
def test_fields_that_hold_people_are_refused(field):
    fake = gateway()
    context = turn(fake)
    outcome = run(get_field_values, context, field=field, cel=None)
    assert outcome.result == {"status": "error", "code": "field_not_allowed"}
    assert fake.requests == []
    assert context.cel_lookups_left == 6


@pytest.mark.parametrize("field", ["name", "labels.team", "service", "byline", "notes"])
def test_other_fields_are_not_on_the_denylist(field):
    assert not get_field_values.is_denied(field)


def test_a_field_named_by_an_address_is_refused():
    assert get_field_values.is_denied("labels.oncall@example.com")


def test_values_that_hold_an_address_are_left_out_of_a_sample():
    alerts = [
        alert("fp-1", labels={"owner": "ops@example.com"}),
        alert("fp-2", labels={"owner": ["db", "Pat <pat@example.org>"]}),
        alert("fp-3", labels={"owner": "@here"}),
    ]
    fake = gateway().on_json(*FIELDS, ["labels.owner"])
    fake.on_json(*QUERY, query_result(*alerts))
    outcome = run(get_field_values, turn(fake), field="labels.owner", cel=None)
    assert outcome.result["values"] == [
        {"value": "db", "count": 1},
        {"value": "@here", "count": 1},
    ]
    assert "example" not in encode_result(outcome.result)


def test_values_that_hold_an_address_are_left_out_of_a_facet():
    fake = gateway().on_json(
        *OPTIONS, options(("critical", 2), ("ops@example.com", 9))
    )
    outcome = run(get_field_values, turn(fake), field="severity", cel=None)
    assert outcome.result["values"] == [{"value": "critical", "count": 2}]


def test_only_a_field_the_context_names():
    fake = gateway()
    context = turn(fake)
    outcome = run(get_field_values, context, field="labels.secret", cel=None)
    assert outcome.result == {"status": "error", "code": "unknown_field"}
    assert context.cel_lookups_left == 6
    assert [r.url.raw_path.decode() for r in fake.requests] == ["/alerts/facets/fields"]


def test_without_budget_no_values_are_read():
    fake = gateway()
    context = turn(fake)
    context.cel_lookups_left = 0
    outcome = run(get_field_values, context, field="severity", cel=None)
    assert outcome.result == {"status": "error", "code": "cel_lookup_limit"}
    assert sent(fake, *OPTIONS) == [] and sent(fake, *QUERY) == []


def test_on_a_read_only_instance_values_are_still_read():
    """The facet values are a POST, which the read-only mode lets through."""
    sample = READ_ONLY["facet_options"]

    async def answer(request):
        assert json.loads(request.content) == sample["request"]["body"]
        return as_response(sample)

    fake = gateway().on(*OPTIONS, answer)
    outcome = run(get_field_values, turn(fake), field="severity", cel=None)
    assert outcome.code == "found"
    assert outcome.result["sampled"] is False


# --- the facets cache ------------------------------------------------------------------


def facet_reads(fake: FakeGateway) -> int:
    return len(sent(fake, *FACETS))


def test_facet_ids_are_cached_per_tenant():
    fake = gateway().on_json(*OPTIONS, options(("critical", 1)))
    run(get_field_values, turn(fake), field="severity", cel=None)
    run(get_field_values, turn(fake), field="severity", cel="name == 'x'")
    assert facet_reads(fake) == 1
    run(get_field_values, turn(fake, tenant_id=OTHER_TENANT), field="severity", cel=None)
    assert facet_reads(fake) == 2
    context_bl.reset_facets_cache()
    run(get_field_values, turn(fake), field="severity", cel=None)
    assert facet_reads(fake) == 3


def test_the_facet_ids_end_with_the_field_names(set_env):
    set_env("ASSISTANT_FIELDS_CACHE_TTL_SECONDS", 0.01)
    fake = gateway().on_json(*OPTIONS, options(("critical", 1)))
    run(get_field_values, turn(fake), field="severity", cel=None)
    asyncio.run(asyncio.sleep(0.05))
    run(get_field_values, turn(fake), field="severity", cel=None)
    assert facet_reads(fake) == 2


def test_the_facets_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(context_bl, "_MAX_CACHED_TENANTS", 3)
    fake = gateway().on_json(*OPTIONS, options(("critical", 1)))
    for number in range(10):
        run(get_field_values, turn(fake, tenant_id=f"t{number}"), field="severity", cel=None)
        assert len(context_bl._facets_cache) <= 3


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (400, INVALID, "invalid_cel"),
        (400, {"detail": "bad"}, "error"),
        (404, {"detail": "gone"}, "error"),
    ],
)
def test_a_refused_facet_read_drops_the_tenants_ids(status, body, code):
    fake = gateway().on_json(*OPTIONS, body, status=status)
    outcome = run(get_field_values, turn(fake), field="severity", cel=None)
    assert outcome.code == code
    assert "keep" not in context_bl._facets_cache
    run(get_field_values, turn(fake), field="severity", cel=None)
    assert facet_reads(fake) == 2


def test_other_failures_keep_the_ids():
    fake = gateway().on_json(*OPTIONS, {"detail": "busy"}, status=503)
    run(get_field_values, turn(fake), field="severity", cel=None)
    assert "keep" in context_bl._facets_cache


def test_a_failed_facets_read_is_not_cached():
    fake = gateway().on_json(*FACETS, {"detail": "no"}, status=502)
    outcome = run(get_field_values, turn(fake), field="severity", cel=None)
    assert outcome.code == "error"
    assert context_bl._facets_cache == {}


def test_the_facets_cache_holds_no_values():
    fake = gateway().on_json(*OPTIONS, options(("critical", 1)))
    run(get_field_values, turn(fake), field="severity", cel=None)
    [(_, ids)] = context_bl._facets_cache.values()
    assert ids == {"severity": SEVERITY_FACET}


def test_a_read_tool_result_never_holds_an_email(monkeypatch):
    """However an alert's people are asked for."""
    email = "canary-reader-77@example.com"
    alerts = [
        alert("fp-1", assignee=email, enriched_by=email, labels={"team": "db"}),
    ]
    fake = (
        gateway()
        .on_json(*QUERY, query_result(*alerts))
        .on_json(*OPTIONS, options(("critical", 1)))
        .on_json(*COUNT, 1)
    )
    context = turn(fake)
    outcomes = [
        run(search_alerts, context, cel="", limit=5, sort=None),
        run(get_alerts, context, fingerprints=["fp-1"]),
        run(get_field_values, context, field="assignee", cel=None),
        run(get_field_values, context, field="enriched_by", cel=None),
        run(get_field_values, context, field="labels.team", cel=None),
        run(count_alerts, context, cel=""),
    ]
    for outcome in outcomes:
        assert email not in encode_result(outcome.result)
    # Nor does anything the tools send.
    assert all(email not in (r.content or b"").decode() for r in fake.requests)
