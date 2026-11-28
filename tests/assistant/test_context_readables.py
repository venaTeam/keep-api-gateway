"""What the context message tells of the user's page and rights: the preset and the
search box of an alerts page, the incident of an incident page, and the drafts they
may confirm."""
import asyncio
import json
import uuid

import pytest
from pydantic import ValidationError

from src.services.assistant.bl import context as context_bl
from src.services.assistant.bl.context import TurnContext, allowed_drafts
from src.services.assistant.bl.prompt import context_message
from src.services.assistant.kinds import registry
from src.services.assistant.kinds.registry import KINDS
from src.services.assistant.models.api.chat import ChatContext, ChatRequest
from src.services.assistant.tools.base import ToolContext
from tests.assistant.fakes import FakeGateway, identity

FIELDS = ("GET", "/alerts/facets/fields")
PRESETS = ("GET", "/preset")
INCIDENT = "6f1c2b9e-3d4a-4c5b-8e7f-0a1b2c3d4e5f"
REQUEST_ID = uuid.uuid4()


def preset(name: str, cel: str, number: int = 1) -> dict:
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "name": name,
        "options": [{"label": "CEL", "value": cel}, {"label": "SQL", "value": {}}],
        "created_by": "someone@example.com",
        "static": False,
    }


@pytest.fixture()
def gateway():
    return (
        FakeGateway()
        .on_json(*FIELDS, ["name"])
        .on_json(
            *PRESETS,
            [
                preset("feed", "", 7),
                preset("Prod (EU)!", 'environment == "prod" && count > 2', 1),
                preset("db", "service == 'db'", 2),
            ],
        )
    )


def gather(gateway, read_once=None, who=None, **context) -> TurnContext:
    return asyncio.run(
        context_bl.gather(
            who or identity(),
            gateway.reader(who),
            ChatContext(**context),
            REQUEST_ID,
            read_once=read_once,
        )
    )


def told(context: TurnContext) -> dict:
    lines = context_message(context)["content"].split("\n")
    assert len(lines) == 3
    return json.loads(lines[1])


def preset_reads(gateway) -> int:
    return sum((r.method, r.url.path) == PRESETS for r in gateway.requests)


# --- active_preset ----------------------------------------------------------------------


def test_the_preset_of_the_page(gateway):
    data = told(gather(gateway, page="/alerts/prod%20(eu)!"))
    assert data["active_preset"] == {
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "Prod (EU)!",
        "cel": 'environment == "prod" && count > 2',
    }
    assert "notes" not in data
    # Who made it is not told.
    assert "someone@example.com" not in context_message(gather(gateway, page="/alerts/db"))[
        "content"
    ]


@pytest.mark.parametrize(
    "page", ["/alerts/feed", "/alerts/feed?cel=x", "/incidents", "", "/alerts/a/b"]
)
def test_no_preset_where_the_page_has_none(gateway, page):
    data = told(gather(gateway, page=page))
    assert "active_preset" not in data
    assert preset_reads(gateway) == 0


def test_a_preset_that_is_not_there_is_told(gateway):
    data = told(gather(gateway, page="/alerts/gone"))
    assert "active_preset" not in data
    assert data["notes"] == ["The preset of the page was not found."]


def test_a_preset_that_cannot_be_read_is_told(gateway):
    gateway.on_json(*PRESETS, {"detail": "no"}, status=500)
    data = told(gather(gateway, page="/alerts/db"))
    assert "active_preset" not in data
    assert data["notes"] == ["The preset of the page could not be read."]


def test_the_presets_are_read_through_the_turns_memo(gateway):
    """A tool that lists the presets later in the turn gets them for nothing."""
    tools = ToolContext(identity=identity(), gateway=gateway.reader())
    gather(gateway, read_once=tools.read_once, page="/alerts/db")
    asyncio.run(tools.read_once(tools.gateway.presets))
    assert preset_reads(gateway) == 1
    assert tools.cel_lookups_left == 6


def test_a_long_filter_is_cut():
    gateway = FakeGateway().on_json(*FIELDS, []).on_json(
        *PRESETS, [preset("long", "count < 5 && " + "a" * 600)]
    )
    data = told(gather(gateway, page="/alerts/long"))
    assert len(data["active_preset"]["cel"]) == 500
    assert data["active_preset"]["cel"].startswith("count < 5")


# --- search_cel ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "shown"),
    [
        ("/alerts/feed?cel=x", True),
        ("/alerts/db", True),
        ("/incidents", False),
        ("/rules", False),
        ("", False),
    ],
)
def test_the_search_box_only_on_an_alerts_page(gateway, page, shown):
    data = told(gather(gateway, page=page, search_cel='name == "disk"'))
    assert ("search_cel" in data) is shown
    if shown:
        assert data["search_cel"] == 'name == "disk"'


@pytest.mark.parametrize(
    "value",
    [
        "x" * 2000,
        'count < 5 && name.contains("a")',
        'name == "équipe"',
    ],
)
def test_a_search_box_filter_up_to_2000_chars(gateway, value):
    data = told(gather(gateway, page="/alerts/feed", search_cel=value))
    assert data["search_cel"] == value


@pytest.mark.parametrize(
    "value",
    ["x" * 2001, "a\nb", "a\tb", "a\x00b", "a\x7fb", "a\x9fb", "\ud800"],
)
def test_a_search_box_filter_with_control_characters_is_a_400(value):
    with pytest.raises(ValidationError):
        ChatRequest.parse_obj(
            {"message": "hi", "context": {"page": "/alerts/feed", "search_cel": value}}
        )


@pytest.mark.parametrize("value", ["a\u202eb", "a\u200bb", "\u2066a\u2069", "a\ufeff"])
def test_a_search_box_filter_that_reads_otherwise_is_dropped_and_told(gateway, value):
    data = told(gather(gateway, page="/alerts/feed", search_cel=value))
    assert "search_cel" not in data
    assert data["notes"] == [
        "The search box holds a filter with invisible characters: it is left out."
    ]


def test_an_empty_search_box_is_none(gateway):
    data = told(gather(gateway, page="/alerts/feed", search_cel="  "))
    assert "search_cel" not in data and "notes" not in data
    assert ChatContext().search_cel == ""


# --- current_incident_id -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "incident"),
    [
        (f"/incidents/{INCIDENT}", INCIDENT),
        (f"/incidents/{INCIDENT}/alerts", INCIDENT),
        (f"/incidents/{INCIDENT}/timeline?x=1", INCIDENT),
        (f"/incidents/{INCIDENT.upper()}", None),
        ("/incidents/not-a-uuid", None),
        ("/incidents", None),
        (f"/alerts/{INCIDENT}", None),
    ],
)
def test_the_incident_of_the_page(gateway, page, incident):
    data = told(gather(gateway, page=page))
    assert data.get("current_incident_id") == incident
    # Not checked against the gateway.
    assert not [r for r in gateway.requests if "incidents" in r.url.path]


# --- allowed_drafts --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scopes", "allowed"),
    [
        (frozenset({"read:*", "write:alert"}), ["change_alert_status"]),
        (frozenset({"read:*", "write:*"}), ["change_alert_status"]),
        (frozenset({"read:*"}), []),
        (frozenset(), []),
        (None, ["change_alert_status"]),
    ],
)
def test_the_drafts_the_scopes_allow(scopes, allowed):
    assert allowed_drafts(scopes) == allowed


def test_a_kind_with_any_permitted_mode_is_allowed(monkeypatch):
    kind = KINDS["change_alert_status"]
    monkeypatch.setattr(
        type(kind), "scope_hint", frozenset({"write:rules", "update:rules"})
    )
    assert allowed_drafts(frozenset({"update:rules"})) == ["change_alert_status"]
    assert allowed_drafts(frozenset({"write:rules"})) == ["change_alert_status"]
    assert allowed_drafts(frozenset({"write:alert"})) == []


def test_a_kind_switched_off_is_not_allowed(gateway, set_env, monkeypatch):
    who = identity(scopes=frozenset({"write:*"}))
    assert told(gather(gateway, who=who))["allowed_drafts"] == ["change_alert_status"]
    set_env("ASSISTANT_DISABLED_KINDS", "change_alert_status")
    assert told(gather(gateway, who=who))["allowed_drafts"] == []
    set_env("ASSISTANT_DISABLED_KINDS", "")
    monkeypatch.setattr(registry, "_disabled_by_code", {"change_alert_status": "test"})
    assert told(gather(gateway, who=who))["allowed_drafts"] == []


def test_the_message_names_kinds_only(gateway):
    content = context_message(gather(gateway, who=identity(email="me@example.com")))[
        "content"
    ]
    assert told(gather(gateway))["allowed_drafts"] == ["change_alert_status"]
    assert "@" not in content and '"keep"' not in content
