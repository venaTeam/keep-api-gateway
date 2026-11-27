"""The user's time zone and the turn's instant: what the model and the tools are told."""
import asyncio
import dataclasses
import json
import logging
import uuid
import zoneinfo
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.services.assistant import lifecycle
from src.services.assistant.bl import context as context_bl
from src.services.assistant.bl.prompt import NO_TIMEZONE_NOTE, context_message
from src.services.assistant.models.api import chat as chat_models
from src.services.assistant.models.api.chat import (
    MAX_TIMEZONE_CHARS,
    ChatContext,
    ChatRequest,
    known_timezones,
)
from src.services.assistant.tools.base import ToolContext, ToolOutcome
from src.services.assistant.tools.change_alert_status import SPEC
from src.services.assistant.tools.registry import TOOLS
from tests.assistant.fakes import FakeGateway, alert, identity, query_result, whoami
from tests.assistant.running import FakeLlm, calls, change, equip, post_chat, says

FIELDS = ("GET", "/alerts/facets/fields")


@pytest.fixture()
def no_cached_zones():
    """`known_timezones` again from what is there, before and after."""
    known_timezones.cache_clear()
    yield
    known_timezones.cache_clear()


def told(context: context_bl.TurnContext) -> dict:
    return json.loads(context_message(context)["content"].split("\n")[1])


def gather(request: ChatContext, **options) -> context_bl.TurnContext:
    gateway = FakeGateway().on_json(*FIELDS, ["name"])
    return asyncio.run(
        context_bl.gather(
            identity(), gateway.reader(), request, uuid.uuid4(), **options
        )
    )


# --- what the browser sends ---------------------------------------------------------


def test_this_server_has_time_zone_data():
    """Without it every zone below is dropped: the live check of the image is L13."""
    assert "Europe/Berlin" in known_timezones()


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("Europe/Berlin", "Europe/Berlin"),
        ("UTC", "UTC"),
        ("Mars/Olympus_Mons", None),
        pytest.param("Europe/" + "B" * (MAX_TIMEZONE_CHARS - 6), None, id="65-chars"),
        pytest.param("x" * 100_000, None, id="100k-chars"),
        ("Europe/Berlin\n", None),
        ("Europe/\x00Berlin", None),
        ("Europe/\x7fBerlin", None),
        ("Europe/Berlin\u202e", None),
        ("\ud800", None),
        ("../../etc/passwd", None),
        ("", None),
        (None, None),
        (60, None),
        (True, None),
        (["Europe/Berlin"], None),
        ({"name": "Europe/Berlin"}, None),
    ],
)
def test_a_zone_never_fails_a_request(sent, kept):
    request = ChatRequest.parse_obj({"message": "hi", "context": {"timezone": sent}})
    assert request.context.timezone == kept
    assert ChatContext().timezone is None


def test_a_name_counts_up_to_its_limit(monkeypatch):
    longest = "Z" * MAX_TIMEZONE_CHARS
    monkeypatch.setattr(
        chat_models, "known_timezones", lambda: frozenset({longest, longest + "Z"})
    )
    assert ChatContext(timezone=longest).timezone == longest
    assert ChatContext(timezone=longest + "Z").timezone is None


def test_without_time_zone_data_every_zone_is_utc(monkeypatch, no_cached_zones):
    monkeypatch.setattr(zoneinfo, "available_timezones", lambda: set())
    assert known_timezones() == frozenset()
    assert ChatContext(timezone="Europe/Berlin").timezone is None


def test_the_zone_names_are_read_once(monkeypatch, no_cached_zones):
    asked = []

    def available():
        asked.append(True)
        return {"Europe/Berlin"}

    monkeypatch.setattr(zoneinfo, "available_timezones", available)
    for _ in range(3):
        ChatContext(timezone="Europe/Berlin")
    assert len(asked) == 1


def test_unreadable_time_zone_data_is_none(monkeypatch, no_cached_zones):
    def broken():
        raise OSError("no tzpath")

    monkeypatch.setattr(zoneinfo, "available_timezones", broken)
    assert known_timezones() == frozenset()


def test_the_start_says_when_there_is_no_time_zone_data(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        lifecycle._check_time_zone_data()
    assert "time zone" not in caplog.text
    monkeypatch.setattr(lifecycle, "known_timezones", lambda: frozenset())
    with caplog.at_level(logging.WARNING):
        lifecycle._check_time_zone_data()
    [record] = [r for r in caplog.records if "time zone" in r.getMessage()]
    assert record.levelname == "WARNING"


# --- the zone and the instant -------------------------------------------------------


def test_the_users_zone():
    assert context_bl.user_zone("Europe/Berlin") == (
        ZoneInfo("Europe/Berlin"),
        "Europe/Berlin",
    )
    assert context_bl.user_zone("UTC") == (ZoneInfo("UTC"), "UTC")
    for name in (None, ""):
        zone, zone_name = context_bl.user_zone(name)
        assert zone is timezone.utc and zone_name == "UTC"


@pytest.mark.parametrize(
    "failure",
    [zoneinfo.ZoneInfoNotFoundError("gone"), ValueError("bad"), OSError("disk")],
)
def test_a_zone_whose_data_cannot_be_read_is_utc(monkeypatch, failure):
    def unreadable(name):
        raise failure

    monkeypatch.setattr(context_bl, "ZoneInfo", unreadable)
    zone, zone_name = context_bl.user_zone("Europe/Berlin")
    assert zone is timezone.utc and zone_name == "UTC"


def test_a_tool_context_of_its_own_is_this_second_in_utc():
    before = datetime.now(timezone.utc).replace(microsecond=0)
    context = ToolContext(identity=identity(), gateway=FakeGateway().reader())
    after = datetime.now(timezone.utc)
    assert before <= context.now <= after
    assert context.now.tzinfo is timezone.utc and context.now.microsecond == 0
    assert context.zone is timezone.utc and context.zone_name == "UTC"


def test_the_context_message_tells_the_zone():
    data = told(gather(ChatContext(timezone="Asia/Kolkata")))
    assert data["timezone"] == "Asia/Kolkata"
    assert "timezone_note" not in data and "notes" not in data


@pytest.mark.parametrize("sent", [None, "Mars/Olympus_Mons"])
def test_the_model_is_told_when_the_zone_is_not_known(sent):
    data = told(gather(ChatRequest.parse_obj({"message": "x", "context": {"timezone": sent}}).context))
    assert data["timezone"] == "UTC"
    assert data["timezone_note"] == NO_TIMEZONE_NOTE
    assert "notes" not in data


def test_the_context_message_shows_the_instant_it_is_given():
    instant = datetime(2026, 11, 27, 9, 30, 5, tzinfo=timezone.utc)
    assert told(gather(ChatContext(), now=instant))["now"] == "2026-11-27T09:30:05+00:00"
    shown = datetime.fromisoformat(told(gather(ChatContext()))["now"])
    assert abs(shown - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert shown.utcoffset() == timedelta(0)


# --- in a turn --------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def gateway():
    return (
        FakeGateway()
        .on_json("GET", "/whoami", whoami())
        .on_json(*FIELDS, ["name"])
        .on_json("POST", "/alerts/query", query_result(alert("fp-1")))
    )


@pytest.fixture()
def seen(monkeypatch) -> list[ToolContext]:
    """The context the tool is given."""
    contexts: list[ToolContext] = []

    async def record(context, arguments):
        contexts.append(context)
        return ToolOutcome("found", {"status": "found"})

    monkeypatch.setitem(TOOLS, SPEC.name, dataclasses.replace(SPEC, run=record))
    return contexts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "zone", "zone_name"),
    [
        ("Europe/Berlin", ZoneInfo("Europe/Berlin"), "Europe/Berlin"),
        ("Atlantis/Capital", timezone.utc, "UTC"),
        (None, timezone.utc, "UTC"),
    ],
)
async def test_the_tools_and_the_model_share_the_turns_instant(
    app, clean_db, gateway, seen, sent, zone, zone_name
):
    llm = FakeLlm(calls(change("fp-1")), says("ok"))
    equip(app, llm, gateway)
    exchange = await post_chat(app, "ack", timezone=sent).finish()
    assert exchange.status == 200
    [context] = seen
    data = json.loads(llm.requests[0].messages[1]["content"].split("\n")[1])
    assert data["now"] == context.now.isoformat(timespec="seconds")
    assert context.now.tzinfo is timezone.utc and context.now.microsecond == 0
    assert (context.zone, context.zone_name) == (zone, zone_name)
    assert data["timezone"] == zone_name
    assert ("timezone_note" in data) is (zone is timezone.utc)
