"""The list tools: presets, correlation rules, dashboards and tags, as the model sees
them, and what they cost."""
import asyncio
import json

import pytest

from src.services.assistant.tools import (
    list_correlation_rules,
    list_dashboards,
    list_presets,
    list_tags,
)
from src.services.assistant.tools.base import (
    MAX_TOOL_RESULT_CHARS,
    ToolContext,
    encode_result,
)
from src.services.assistant.tools.read_common import MAX_LIST_ROWS
from src.services.assistant.tools.registry import TOOLS
from tests.assistant.fakes import FakeGateway, identity
from tests.assistant.test_gateway_samples import SAMPLES, as_response

PRESETS = ("GET", "/preset")
RULES = ("GET", "/rules")
DASHBOARDS = ("GET", "/dashboard")
TAGS = ("GET", "/tags")
EMAIL = "owner@example.com"
LIST_TOOLS = {
    "list_presets": (list_presets.SPEC, "Reading presets"),
    "list_correlation_rules": (list_correlation_rules.SPEC, "Reading correlation rules"),
    "list_dashboards": (list_dashboards.SPEC, "Reading dashboards"),
    "list_tags": (list_tags.SPEC, "Reading tags"),
}


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def preset(number: int, name: str, **fields) -> dict:
    return {
        "id": uid(number),
        "name": name,
        "options": [
            {"label": "CEL", "value": f'service == "s{number}"'},
            {"label": "SQL", "value": {"sql": "service = :s", "params": {"s": "x"}}},
        ],
        "created_by": EMAIL,
        "is_private": False,
        "is_noisy": False,
        "should_do_noise_now": False,
        "counter_shows_firing_only": False,
        "static": False,
        "tags": [{"id": "t-1", "name": "ops"}],
        **fields,
    }


def rule(number: int, name: str, **fields) -> dict:
    return {
        "id": uid(number),
        "name": name,
        "definition_cel": 'service == "orders"',
        "definition": {"sql": "service = :service", "params": {"service": "orders"}},
        "definition_cel_ast": {"node_type": "ComparisonNode"},
        "distribution": [{"hour": 1, "number": 3}],
        "timeframe": 600,
        "timeunit": "minutes",
        "grouping_criteria": ["service"],
        "create_on": "any",
        "resolve_on": "never",
        "require_approve": False,
        "threshold": 1,
        "incident_name_template": None,
        "incident_prefix": None,
        "incidents": 4,
        "assignee": EMAIL,
        "created_by": EMAIL,
        "updated_by": EMAIL,
        "tenant_id": "keep",
        "group_description": "carried",
        "multi_level": False,
        "multi_level_property_name": None,
        **fields,
    }


def dashboard(number: int, name: str, widgets: int = 1) -> dict:
    return {
        "id": uid(number),
        "dashboard_name": name,
        "dashboard_config": {
            "layout": [],
            "widgetData": [
                {
                    "i": f"w-{n}",
                    "name": f"Widget {n}",
                    "widgetType": "PRESET",
                    "preset": {"name": "critical", "created_by": EMAIL},
                }
                for n in range(widgets)
            ],
        },
        "created_by": EMAIL,
        "updated_by": EMAIL,
    }


def turn(fake: FakeGateway) -> ToolContext:
    who = identity(email="ops@example.com")
    return ToolContext(identity=who, gateway=fake.reader(who))


def run(tool, context: ToolContext, **arguments):
    """`tool`: its module, or its ToolSpec."""
    spec = getattr(tool, "SPEC", tool)
    return asyncio.run(spec.run(context, json.dumps(arguments)))


def requests(fake: FakeGateway, method: str, path: str) -> int:
    return sum(
        (r.method, r.url.raw_path.decode()) == (method, path) for r in fake.requests
    )


# --- what they are ------------------------------------------------------------------


def test_what_each_list_tool_is():
    for name, (spec, label) in LIST_TOOLS.items():
        assert TOOLS[name] is spec
        assert (spec.name, spec.label) == (name, label)
        assert spec.draft_kind is None and spec.makes_link is False, name
        assert spec.prompt_guidance.startswith(f"{name}:"), name


@pytest.mark.parametrize("tool", [list_presets, list_correlation_rules, list_dashboards])
def test_the_name_filter_may_be_null(tool):
    schema = tool.SPEC.parameters
    assert schema["required"] == ["name_contains"]
    assert "null" in schema["properties"]["name_contains"]["type"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (list_presets, {"name_contains": ["pay"]}),
        (list_presets, {"name_contains": "x" * 257}),
        (list_presets, {"name_contains": None, "extra": 1}),
        (list_correlation_rules, {"name_contains": "a\x00b"}),
        (list_dashboards, {"name": "x"}),
        (list_tags, {"name_contains": "x"}),
    ],
)
def test_arguments_outside_the_schema_are_refused_before_any_read(tool, arguments):
    fake = FakeGateway()
    context = turn(fake)
    outcome = run(tool, context, **arguments)
    assert outcome.code == "invalid_arguments"
    assert fake.requests == []
    assert context.cel_lookups_left == 6


# --- presets ------------------------------------------------------------------------


def test_presets_as_rows():
    fake = FakeGateway().on_json(
        *PRESETS,
        [
            preset(1, "Payments", created_by="ops@example.com", is_private=True),
            preset(2, "feed", static=True, created_by=None, options=[]),
        ],
    )
    context = turn(fake)
    outcome = run(list_presets, context, name_contains=None)
    assert outcome.code == "found"
    assert outcome.result == {
        "status": "found",
        "rows": [
            {
                "id": uid(2),
                "name": "feed",
                "cel": None,
                "is_private": False,
                "is_noisy": False,
                "counter_shows_firing_only": False,
                "tags": ["ops"],
                "static": True,
                "mine": False,
            },
            {
                "id": uid(1),
                "name": "Payments",
                "cel": 'service == "s1"',
                "is_private": True,
                "is_noisy": False,
                "counter_shows_firing_only": False,
                "tags": ["ops"],
                "static": False,
                "mine": True,
            },
        ],
        "total": 2,
        "more": False,
    }
    # No filter budget; the same list again in the turn is not read again.
    run(list_presets, context, name_contains="pay")
    assert requests(fake, *PRESETS) == 1
    assert context.cel_lookups_left == 6


def test_a_name_filter_ignores_case():
    fake = FakeGateway().on_json(
        *PRESETS, [preset(1, "Payments EU"), preset(2, "Orders"), preset(3, "eu-pay")]
    )
    outcome = run(list_presets, turn(fake), name_contains="  PAY ")
    assert [r["name"] for r in outcome.result["rows"]] == ["eu-pay", "Payments EU"]
    assert (outcome.result["total"], outcome.result["more"]) == (2, False)


def test_none_found_is_still_a_list():
    fake = FakeGateway().on_json(*PRESETS, [preset(1, "Orders")])
    outcome = run(list_presets, turn(fake), name_contains="payments")
    assert (outcome.code, outcome.result) == (
        "found",
        {"status": "found", "rows": [], "total": 0, "more": False},
    )


def test_a_filter_and_a_name_are_cut_and_stay_readable():
    long_cel = "count < 5 && " + "a" * 400
    fake = FakeGateway().on_json(
        *PRESETS,
        [
            preset(
                1,
                "a<b>\n" + "n" * 300,
                options=[{"label": "CEL", "value": long_cel}],
            )
        ],
    )
    [row] = run(list_presets, turn(fake), name_contains=None).result["rows"]
    assert row["cel"] == long_cel[:200]
    assert row["name"] == ("a<b> " + "n" * 300)[:256]
    # Escaped in the JSON, not replaced: the model reads them as written.
    assert json.loads(encode_result({"rows": [row]}))["rows"][0] == row


# --- fifty rows ---------------------------------------------------------------------


SOURCES = {
    "list_presets": (PRESETS, lambda n: preset(n, f"Preset {n:02d} " + "x" * 200)),
    "list_correlation_rules": (RULES, lambda n: rule(n, f"Rule {n:02d} " + "x" * 200)),
    "list_dashboards": (DASHBOARDS, lambda n: dashboard(n, f"Board {n:02d}", 20)),
    "list_tags": (TAGS, lambda n: {"id": uid(n), "name": f"tag {n:02d} " + "x" * 300}),
}


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_fifty_rows_are_twenty_and_more(name):
    (method, path), make = SOURCES[name]
    fake = FakeGateway().on_json(method, path, [make(n) for n in range(50, 0, -1)])
    arguments = {} if name == "list_tags" else {"name_contains": None}
    outcome = run(TOOLS[name], turn(fake), **arguments)
    result = outcome.result
    assert (outcome.code, result["status"]) == ("found", "found")
    assert (result["total"], result["more"]) == (50, True)
    assert len(result["rows"]) == MAX_LIST_ROWS
    # By name.
    first = result["rows"][0]
    assert (first if name == "list_tags" else first["name"]).startswith(
        ("Preset 01", "Rule 01", "Board 01", "tag 01")
    )
    if name == "list_tags":
        # Names are cut to 256 chars: twenty of them fit one tool message.
        assert json.loads(encode_result(result)) == result
        return
    # Too long for one tool message: still JSON, cut and marked as such.
    assert len(json.dumps(result)) > MAX_TOOL_RESULT_CHARS
    encoded = encode_result(result)
    assert len(encoded) <= MAX_TOOL_RESULT_CHARS
    told = json.loads(encoded)
    assert told["truncated"] is True
    assert (told["total"], told["more"]) == (50, True)
    assert 0 < len(told["rows"]) < MAX_LIST_ROWS


# --- correlation rules --------------------------------------------------------------


def test_rules_as_rows_cost_one_unit_once():
    fake = FakeGateway().on_json(*RULES, [rule(1, "Orders", incident_prefix="ORD")])
    context = turn(fake)
    outcome = run(list_correlation_rules, context, name_contains=None)
    assert outcome.result["rows"] == [
        {
            "id": uid(1),
            "name": "Orders",
            "cel": 'service == "orders"',
            "timeframe_seconds": 600,
            "grouping_criteria": ["service"],
            "create_on": "any",
            "resolve_on": "never",
            "require_approve": False,
            "threshold": 1,
            "incident_name_template": None,
            "incident_prefix": "ORD",
            "incidents": 4,
        }
    ]
    assert context.cel_lookups_left == 5
    run(list_correlation_rules, context, name_contains="ord")
    assert requests(fake, *RULES) == 1
    assert context.cel_lookups_left == 5


def test_an_unreadable_rule():
    fake = FakeGateway().on_json(*RULES, {"message": "boom"}, status=500)
    outcome = run(list_correlation_rules, turn(fake), name_contains=None)
    assert (outcome.code, outcome.result) == (
        "error",
        {"status": "error", "code": "rules_unreadable"},
    )
    # The chat goes on: only the Correlations page can fix it.
    assert outcome.stream_error is None


def test_without_budget_no_rule_is_read():
    fake = FakeGateway().on_json(*RULES, [rule(1, "Orders")])
    context = turn(fake)
    context.cel_lookups_left = 0
    outcome = run(list_correlation_rules, context, name_contains=None)
    assert outcome.result == {"status": "error", "code": "cel_lookup_limit"}
    assert fake.requests == []


# --- dashboards and tags -------------------------------------------------------------


def test_dashboards_as_rows():
    broken = {"id": uid(3), "dashboard_name": "Broken", "dashboard_config": None}
    fake = FakeGateway().on_json(
        *DASHBOARDS, [dashboard(1, "Ops", widgets=25), broken]
    )
    rows = run(list_dashboards, turn(fake), name_contains=None).result["rows"]
    assert rows[0] == {
        "id": uid(3),
        "name": "Broken",
        "widget_count": 0,
        "widgets": [],
    }
    assert rows[1]["widget_count"] == 25
    assert rows[1]["widgets"][:2] == [
        {"title": "Widget 0", "type": "PRESET"},
        {"title": "Widget 1", "type": "PRESET"},
    ]
    assert len(rows[1]["widgets"]) == 20


def test_tags_by_name_only():
    fake = FakeGateway().on_json(
        *TAGS,
        [
            {"id": "t-2", "name": "prod", "tenant_id": "keep"},
            {"id": "t-1", "name": "Ops", "tenant_id": "keep"},
            {"id": "t-3", "name": "prod", "tenant_id": "keep"},
        ],
    )
    outcome = run(list_tags, turn(fake))
    assert outcome.result == {
        "status": "found",
        "rows": ["Ops", "prod"],
        "total": 2,
        "more": False,
    }


# --- what never reaches the model -----------------------------------------------------


def test_no_email_reaches_the_model():
    fake = (
        FakeGateway()
        .on_json(*PRESETS, [preset(1, "Payments")])
        .on_json(*RULES, [rule(1, "Orders")])
        .on_json(*DASHBOARDS, [dashboard(1, "Ops")])
        .on_json(*TAGS, [{"id": "t-1", "name": "ops", "tenant_id": "keep"}])
    )
    context = turn(fake)
    told = "".join(
        encode_result(run(TOOLS[name], context, **arguments).result)
        for name, arguments in (
            ("list_presets", {"name_contains": None}),
            ("list_correlation_rules", {"name_contains": None}),
            ("list_dashboards", {"name_contains": None}),
            ("list_tags", {}),
        )
    )
    assert "@" not in told
    for hidden in ("created_by", "updated_by", "assignee", "tenant_id", "keep", "carried"):
        assert hidden not in told, hidden
    assert "distribution" not in told and "ComparisonNode" not in told


@pytest.mark.parametrize("name", sorted(SOURCES))
@pytest.mark.parametrize(
    ("status", "code", "stream_error"),
    [
        (401, "unauthenticated", "unauthenticated"),
        (403, "error", None),
        (502, "error", "gateway_unavailable"),
    ],
)
def test_a_failed_list(name, status, code, stream_error):
    (method, path), _ = SOURCES[name]
    fake = FakeGateway().on_json(method, path, {"detail": "no"}, status=status)
    arguments = {} if name == "list_tags" else {"name_contains": None}
    outcome = run(TOOLS[name], turn(fake), **arguments)
    assert (outcome.code, outcome.stream_error) == (code, stream_error)


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_the_gateways_own_answers(name):
    """Captured from the real routes (tests/test_assistant_gateway_samples.py)."""
    (method, path), _ = SOURCES[name]
    sample = {"list_presets": "presets", "list_correlation_rules": "rules",
              "list_dashboards": "dashboards", "list_tags": "tags"}[name]
    answer = SAMPLES["read_write"][sample]
    assert (answer["request"]["method"], answer["request"]["path"]) == (method, path)
    fake = FakeGateway().on(method, path, as_response(answer))
    arguments = {} if name == "list_tags" else {"name_contains": None}
    outcome = run(TOOLS[name], turn(fake), **arguments)
    assert outcome.code == "found"
    assert outcome.result["total"] == len(answer["body"])
    assert "@" not in encode_result(outcome.result)
