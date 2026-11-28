"""open_page: a link the user clicks, to a page of spec 3.4 only, built by
navigation.py after the page's object was found; refused to a NOC user outside the
alerts pages. And how a turn sends it."""
import asyncio
import json
from typing import get_args

import pytest

from src.services.assistant import navigation
from src.services.assistant.bl.timeline import LINK_RESULT_PREFIX
from src.services.assistant.core import metrics
from src.services.assistant.models.api.stream import STREAM_EVENTS, LinkEvent, LinkPage
from src.services.assistant.tools import open_page
from src.services.assistant.tools.base import ToolContext, encode_result
from src.services.assistant.tools.registry import TOOLS
from tests.assistant.fakes import FakeGateway, identity, whoami
from tests.assistant.golden_stream import FIXTURE
from tests.assistant.rows import messages
from tests.assistant.running import Exchange, FakeLlm, calls, equip, post_chat, says

PRESETS = ("GET", "/preset")
RULES = ("GET", "/rules")
DASHBOARDS = ("GET", "/dashboard")
CHECK = ("POST", "/cel/validate")
RULE_ID = "0b3c2d1e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
EDITOR_SCOPES = frozenset({"read:*", "write:*", "delete:*", "update:*", "execute:*"})
NOC_SCOPES = frozenset({"read:*", "write:alert"})

# Who asks: (role from whoami, scopes) and whether they count as a NOC user.
WHO = {
    "noc": ("noc", NOC_SCOPES, True),
    "viewer": ("viewer", frozenset({"read:*"}), False),
    "editor": ("editor", EDITOR_SCOPES, False),
    "admin": ("admin", EDITOR_SCOPES, False),
    "no_role_alert_writes_only": (None, NOC_SCOPES, True),
    "no_role_read_only": (None, frozenset({"read:*"}), True),
    "no_role_editor": (None, EDITOR_SCOPES, False),
    "no_role_one_rule_write": (None, frozenset({"read:*", "write:rules"}), False),
    "no_role_unknown_scopes": (None, None, False),
}

# A parameter each page finds in `keep()`.
PARAMS = {
    "alerts_feed": None,
    "alerts_preset": "Payments",
    "alerts_search": 'severity == "critical"',
    "incidents": None,
    "rules": None,
    "rule": RULE_ID,
    "dashboard": "NOC overview",
}


def keep() -> FakeGateway:
    return (
        FakeGateway()
        .on_json(*PRESETS, [{"id": "p-1", "name": "Payments", "static": False}])
        .on_json(*RULES, [{"id": RULE_ID, "name": "Orders", "incidents": 2}])
        .on_json(*DASHBOARDS, [{"id": 1, "dashboard_name": "NOC overview"}])
        .on_json(*CHECK, {"valid": True, "diagnostics": []})
    )


def turn(fake: FakeGateway, who: str = "editor") -> ToolContext:
    role, scopes, _ = WHO[who]
    person = identity(role=role, scopes=scopes)
    return ToolContext(identity=person, gateway=fake.reader(person))


def run(context: ToolContext, **arguments):
    return asyncio.run(open_page.SPEC.run(context, json.dumps(arguments)))


def link_to(context: ToolContext, page: str, param=None, label="Open it"):
    return run(context, page=page, param=param, label=label)


# --- what it is ----------------------------------------------------------------------


def test_what_the_tool_is():
    spec = TOOLS["open_page"]
    assert spec is open_page.SPEC
    assert (spec.makes_link, spec.draft_kind) == (True, None)
    assert spec.label == "Preparing a link"
    assert spec.prompt_guidance.startswith("open_page:")
    schema = spec.parameters
    assert schema["required"] == ["page", "param", "label"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["page"]["enum"] == list(navigation.PAGES)
    assert "null" in schema["properties"]["param"]["type"]


def test_the_linkable_pages_are_the_spec_table():
    # rules/assistant.md "Links (from v1)": exactly these, in this order.
    assert tuple(navigation.PAGES) == (
        "alerts_feed",
        "alerts_preset",
        "alerts_search",
        "incidents",
        "rules",
        "rule",
        "dashboard",
    )
    assert get_args(LinkPage) == tuple(navigation.PAGES)
    assert LinkEvent in STREAM_EVENTS
    assert LinkEvent.event == "link"


# --- the NOC matrix ------------------------------------------------------------------


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("page", navigation.PAGES)
def test_the_noc_matrix(page, who):
    fake = keep()
    context = turn(fake, who)
    outcome = link_to(context, page, PARAMS[page])
    refused = WHO[who][2] and not navigation.noc_allowed(page)
    if refused:
        assert (outcome.code, outcome.result) == (
            "error",
            {"status": "error", "code": "page_not_available"},
        )
        assert outcome.link is None and outcome.stream_error is None
        assert fake.requests == []
        assert context.cel_lookups_left == 6
    else:
        assert outcome.code == "link_ready", outcome.result
        assert outcome.link.path == navigation.build(page, PARAMS[page])


def test_the_noc_pages_are_the_alerts_pages():
    allowed = {page for page in navigation.PAGES if navigation.noc_allowed(page)}
    assert allowed == {"alerts_feed", "alerts_preset", "alerts_search"}


# --- the link ------------------------------------------------------------------------


def test_a_link_is_built_by_navigation_only():
    context = turn(keep())
    outcome = link_to(context, "alerts_preset", "payments", "Open Payments")
    path = "/alerts/payments"
    assert outcome.code == "link_ready"
    assert outcome.result == {
        "status": "link_ready",
        "link": {
            "page": "alerts_preset",
            "param": "payments",
            "path": path,
            "label": "Open Payments",
        },
    }
    assert (outcome.link.page, outcome.link.param, outcome.link.path) == (
        "alerts_preset",
        "payments",
        path,
    )
    assert outcome.link.label == "Open Payments"
    # The timeline finds the stored row by how it begins.
    assert encode_result(outcome.result).startswith(LINK_RESULT_PREFIX)


def test_the_parameter_is_the_one_the_page_reads_back():
    """keep-ui finds a preset by its lowercased name: the link says so too."""
    outcome = link_to(turn(keep()), "alerts_preset", "PAYMENTS")
    assert (outcome.link.param, outcome.link.path) == ("payments", "/alerts/payments")
    assert navigation.parse(outcome.link.path) == ("alerts_preset", "payments")


@pytest.mark.parametrize(
    ("page", "param"),
    [
        ("alerts_feed", None),
        ("incidents", None),
        ("rules", None),
        ("alerts_search", 'name.contains("disk") && severity == "critical"'),
        ("dashboard", "NOC overview"),
        ("rule", RULE_ID),
    ],
)
def test_each_link_reads_back(page, param):
    outcome = link_to(turn(keep()), page, param)
    assert outcome.code == "link_ready"
    assert navigation.parse(outcome.link.path) == (page, outcome.link.param)


def test_the_label_shows_only_what_can_be_seen():
    hidden = "\u202e\u200b\u2066\u2069\ufeff"
    outcome = link_to(turn(keep()), "alerts_feed", None, f"Open{hidden} the feed")
    assert outcome.link.label == "Open the feed"
    assert outcome.result["link"]["label"] == "Open the feed"


def test_a_label_that_shows_nothing_is_refused():
    outcome = link_to(turn(keep()), "alerts_feed", None, "\u200b\u2066")
    assert outcome.result == {"status": "error", "code": "label_not_visible"}
    assert outcome.link is None


# --- what it checks ------------------------------------------------------------------


def requests(fake: FakeGateway, method: str, path: str) -> int:
    return sum(
        (r.method, r.url.raw_path.decode()) == (method, path) for r in fake.requests
    )


def test_a_rule_page_costs_one_filter_unit_once():
    fake = keep()
    context = turn(fake)
    assert link_to(context, "rule", RULE_ID).code == "link_ready"
    assert context.cel_lookups_left == 5
    assert link_to(context, "rule", RULE_ID).code == "link_ready"
    assert requests(fake, *RULES) == 1
    assert context.cel_lookups_left == 5


def test_the_other_checks_cost_no_filter_unit():
    fake = keep()
    context = turn(fake)
    for page in ("alerts_preset", "alerts_search", "dashboard"):
        assert link_to(context, page, PARAMS[page]).code == "link_ready"
    assert context.cel_lookups_left == 6
    assert [requests(fake, *route) for route in (PRESETS, CHECK, DASHBOARDS)] == [
        1,
        1,
        1,
    ]
    [check] = [r for r in fake.requests if r.url.raw_path.decode() == CHECK[1]]
    assert json.loads(check.content) == {
        "cel": 'severity == "critical"',
        "context": "alerts",
    }


def test_without_budget_no_rule_is_read():
    fake = keep()
    context = turn(fake)
    context.cel_lookups_left = 0
    outcome = link_to(context, "rule", RULE_ID)
    assert outcome.result == {"status": "error", "code": "cel_lookup_limit"}
    assert fake.requests == []


@pytest.mark.parametrize(
    ("page", "param"),
    [
        ("alerts_preset", "Refunds"),
        ("dashboard", "noc overview"),
        ("rule", "00000000-0000-4000-8000-000000000009"),
    ],
)
def test_an_object_that_is_not_there(page, param):
    outcome = link_to(turn(keep()), page, param)
    assert (outcome.code, outcome.result) == ("not_found", {"status": "not_found"})
    assert outcome.link is None


def test_a_search_keep_cannot_read():
    fake = keep().on_json(
        *CHECK, {"valid": False, "diagnostics": [{"message": "Syntax error"}]}
    )
    outcome = link_to(turn(fake), "alerts_search", "severity ==")
    assert outcome.result == {"status": "invalid_cel", "diagnostics": ["Syntax error"]}
    assert outcome.link is None


@pytest.mark.parametrize(
    ("page", "param", "code"),
    [
        ("alerts_feed", "x", "param_not_used"),
        ("incidents", "x", "param_not_used"),
        ("alerts_preset", None, "param_not_usable"),
        ("alerts_preset", "feed", "param_not_usable"),
        ("alerts_preset", "..", "param_not_usable"),
        ("dashboard", "a%2Fb", "param_not_usable"),
        ("rule", RULE_ID.upper(), "param_not_usable"),
        ("rule", "7", "param_not_usable"),
        ("alerts_search", "a\nb", "param_not_usable"),
    ],
)
def test_parameters_a_page_cannot_take_are_refused_before_any_read(page, param, code):
    fake = keep()
    context = turn(fake)
    outcome = link_to(context, page, param)
    assert outcome.result == {"status": "error", "code": code}
    assert fake.requests == []
    assert context.cel_lookups_left == 6


@pytest.mark.parametrize(
    "arguments",
    [
        {"page": "workflows", "param": None, "label": "x"},
        {"page": "settings", "param": None, "label": "x"},
        {"page": "alerts_feed", "param": None, "label": "x" * 81},
        {"page": "alerts_feed", "param": None, "label": ""},
        {"page": "alerts_feed", "param": None, "label": "x", "path": "/alerts"},
        {"page": "alerts_feed", "param": None},
        {"page": "alerts_preset", "param": "a\x00b", "label": "x"},
    ],
)
def test_arguments_outside_the_schema(arguments):
    fake = keep()
    outcome = run(turn(fake), **arguments)
    assert outcome.code == "invalid_arguments"
    assert fake.requests == []


@pytest.mark.parametrize(
    ("status", "code", "stream_error"),
    [(500, "rules_unreadable", None), (503, "gateway_unavailable", "gateway_unavailable")],
)  # fmt: skip
def test_a_rule_read_that_fails(status, code, stream_error):
    fake = keep().on_json(*RULES, {"message": "boom"}, status=status)
    outcome = link_to(turn(fake), "rule", RULE_ID)
    assert outcome.result == {"status": "error", "code": code}
    assert outcome.stream_error == stream_error


# --- the fixture ---------------------------------------------------------------------


def fixture_links() -> list[dict]:
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sent = [
        event["data"]
        for stream in golden["streams"].values()
        for event in stream["events"]
        if event["event"] == "link"
    ]
    stored = [
        item
        for conversation in golden["conversations"].values()
        for item in conversation["items"]
        if item["type"] == "link"
    ]
    return sent + stored


def test_every_link_of_the_fixture_reads_back():
    links = fixture_links()
    assert {link["page"] for link in links} == set(navigation.PAGES)
    for link in links:
        assert navigation.parse(link["path"]) == (link["page"], link["param"]), link


# --- in a turn -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


def counter(page: str) -> float:
    return metrics.links_total.labels(page=page)._value.get()


def chat_gateway(**who) -> FakeGateway:
    return (
        keep()
        .on_json("GET", "/whoami", whoami(**who))
        .on_json("GET", "/alerts/facets/fields", ["name"])
    )


async def view(app, conversation_id) -> dict:
    exchange = await Exchange(
        app, "GET", f"/assistant/conversations/{conversation_id}"
    ).finish()
    assert exchange.status == 200, exchange.text
    return exchange.json()


@pytest.mark.asyncio
async def test_a_turn_sends_the_link_after_its_row_and_shows_it_again(app, db):
    arguments = {"page": "alerts_preset", "param": "Payments", "label": "Open it"}
    llm = FakeLlm(calls(arguments, name="open_page"), says("Here it is."))
    equip(app, llm, chat_gateway())
    before = counter("alerts_preset")
    exchange = await post_chat(app, "show payments").finish()

    names = exchange.names
    started = names.index("tool_status")
    assert names[started : started + 3] == ["tool_status", "link", "tool_status"]
    link = exchange.event("link")
    conversation = exchange.event("conversation")["conversation_id"]
    [row] = [m for m in messages(db, conversation) if m["role"] == "tool"]
    assert link == {
        "id": str(row["id"]),
        "page": "alerts_preset",
        "param": "payments",
        "path": "/alerts/payments",
        "label": "Open it",
    }
    assert row["draft_id"] is None
    assert row["content"].startswith(LINK_RESULT_PREFIX)
    assert counter("alerts_preset") == before + 1

    body = await view(app, conversation)
    [item] = [i for i in body["items"] if i["type"] == "link"]
    assert item == {"type": "link", **link, "created_at": item["created_at"]}
    assert [i["type"] for i in body["items"]] == ["message", "link", "message"]


@pytest.mark.asyncio
async def test_a_noc_user_gets_no_link_to_a_dashboard(app, db):
    arguments = {"page": "dashboard", "param": "NOC overview", "label": "Open it"}
    llm = FakeLlm(calls(arguments, name="open_page"), says("I cannot."))
    equip(app, llm, chat_gateway())  # whoami's role is noc
    before = counter("dashboard")
    exchange = await post_chat(app, "open the dashboard").finish()
    assert "link" not in exchange.names
    finished = [
        data["outcome"]
        for name, data in exchange.events
        if name == "tool_status" and data["phase"] == "finished"
    ]
    assert finished == ["error"]
    assert counter("dashboard") == before
    conversation = exchange.event("conversation")["conversation_id"]
    body = await view(app, conversation)
    assert [i["type"] for i in body["items"]] == ["message", "message"]
