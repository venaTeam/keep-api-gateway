"""What one chat turn may do: rounds, calls, drafts, links, filters and time. Each cap
counts calls of the tools it is about, over the whole turn."""
import dataclasses
import json
import time

import pytest

from src.services.assistant.bl import chat_turn
from src.services.assistant.core import metrics
from src.services.assistant.llm.client import Finished, ToolCallComplete, Usage
from src.services.assistant.tools import base
from src.services.assistant.tools.base import ToolOutcome, ToolSpec
from src.services.assistant.tools.change_alert_status import SPEC
from src.services.assistant.tools.registry import TOOLS
from tests.assistant.fakes import FakeGateway, alert, query_result, whoami
from tests.assistant.running import (
    FakeClock,
    FakeLlm,
    calls,
    change,
    equip,
    post_chat,
    says,
)

QUERY = ("POST", "/alerts/query")


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def gateway():
    return (
        FakeGateway()
        .on_json("GET", "/whoami", whoami())
        .on_json("GET", "/alerts/facets/fields", ["name"])
        .on_json(*QUERY, query_result(alert("fp-1"), alert("fp-2")))
    )


class FakeTool:
    """A tool that answers with `code` and remembers what it saw."""

    def __init__(self, name: str, code: str = "found", **spec):
        self.runs = 0
        self.deadlines: list[float | None] = []
        self.code = code
        self.spec = dataclasses.replace(
            SPEC,
            name=name,
            label=f"Running {name}",
            parameters={"type": "object", "properties": {}},
            run=self.run,
            draft_kind=None,
            **spec,
        )

    async def run(self, context, arguments) -> ToolOutcome:
        self.runs += 1
        # The reader the turn hands its tools, and one for a draft made from it.
        self.deadlines.append(context.gateway._caller.deadline_at)
        self.deadlines.append(
            context.gateway.for_request("00000000-0000-4000-8000-0000000000d1")
            ._caller.deadline_at
        )
        return ToolOutcome(self.code, {"status": self.code})


@pytest.fixture()
def reads(monkeypatch) -> FakeTool:
    tool = FakeTool("get_alerts")
    monkeypatch.setitem(TOOLS, tool.spec.name, tool.spec)
    return tool


@pytest.fixture()
def links(monkeypatch) -> FakeTool:
    tool = FakeTool("open_page", "link_ready", makes_link=True)
    monkeypatch.setitem(TOOLS, tool.spec.name, tool.spec)
    return tool


def replies(request) -> list[dict]:
    """The tool results the model got back in this request, in order."""
    return [
        json.loads(m["content"].split("\n")[1])
        for m in request.messages
        if m["role"] == "tool"
    ]


def outcomes(exchange) -> list[str]:
    return [
        data["outcome"]
        for name, data in exchange.events
        if name == "tool_status" and data["phase"] == "finished"
    ]


def counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def test_the_caps():
    assert base.MAX_FILTER_LOOKUPS_PER_TURN == 6
    assert base.MAX_LINKS_PER_TURN == 3
    assert (chat_turn.TOOLS_OFF_SECONDS, chat_turn.NESTED_CALL_MARGIN_SECONDS) == (
        20.0,
        15.0,
    )
    assert SPEC.draft_kind == SPEC.name and SPEC.makes_link is False
    assert ToolSpec.__dataclass_fields__["draft_kind"].default is None
    assert ToolSpec.__dataclass_fields__["makes_link"].default is False


# --- drafts ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_round_of_four_draft_calls_makes_three_drafts(
    app, db, gateway, no_read_tools
):
    llm = FakeLlm(
        calls(change("fp-1"), change("fp-1"), change("fp-2"), change("fp-2")),
        says("Three of them."),
    )
    equip(app, llm, gateway)
    exchange = await post_chat(app, "ack").finish()
    assert exchange.names.count("draft") == 3
    assert outcomes(exchange) == ["draft_created"] * 3 + ["error"]
    assert replies(llm.requests[1])[-1] == {"status": "error", "code": "draft_limit"}
    # Nothing left that could do anything.
    assert [r.tool_choice for r in llm.requests] == ["auto", "none"]
    assert exchange.event("done")["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_reads_still_answer_after_three_drafts(app, db, gateway, reads):
    llm = FakeLlm(
        calls(change("fp-1"), change("fp-1"), change("fp-2")),
        # A read and a fourth draft in one round: only the draft is refused.
        [
            ToolCallComplete("call_4", "get_alerts", "{}"),
            ToolCallComplete("call_5", SPEC.name, json.dumps(change("fp-2"))),
            Usage(100, 10),
            Finished("tool_calls"),
        ],
        says("Done."),
    )
    equip(app, llm, gateway)
    exchange = await post_chat(app, "ack").finish()
    assert exchange.names.count("draft") == 3
    assert reads.runs == 1
    assert outcomes(exchange) == ["draft_created"] * 3 + ["found", "error"]
    assert replies(llm.requests[2])[-2:] == [
        {"status": "found"},
        {"status": "error", "code": "draft_limit"},
    ]
    assert [r.tool_choice for r in llm.requests] == ["auto", "auto", "auto"]
    assert exchange.event("done")["finish_reason"] == "stop"


# --- filters -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_seventh_filter_of_a_turn_gets_the_budget_error(app, db, gateway):
    gateway.on_json(*QUERY, query_result())
    by_cel = change(cel="true")
    llm = FakeLlm(
        calls(*[by_cel] * 3), calls(*[by_cel] * 4, first=4), says("None match.")
    )
    equip(app, llm, gateway)
    await post_chat(app, "ack").finish()
    assert gateway.paths().count("POST /alerts/query") == 6
    last = replies(llm.requests[2])
    assert last[-1] == {"status": "error", "code": "cel_lookup_limit"}
    assert [r["status"] for r in last[:-1]] == ["no_matches"] * 6


# --- links -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fourth_link_is_refused(app, db, gateway, links):
    llm = FakeLlm(calls({}, {}, {}, {}, name="open_page"), says("Here."))
    equip(app, llm, gateway)
    exchange = await post_chat(app, "links").finish()
    assert links.runs == 3
    assert outcomes(exchange) == ["link_ready"] * 3 + ["error"]
    assert replies(llm.requests[1])[-1] == {"status": "error", "code": "link_limit"}
    # The draft tool can still do something.
    assert [r.tool_choice for r in llm.requests] == ["auto", "auto"]


@pytest.mark.asyncio
async def test_only_a_link_that_was_made_counts(app, db, gateway, links):
    links.code = "not_found"
    llm = FakeLlm(calls({}, {}, {}, {}, name="open_page"), says("None."))
    equip(app, llm, gateway)
    exchange = await post_chat(app, "links").finish()
    assert links.runs == 4
    assert outcomes(exchange) == ["not_found"] * 4


@pytest.mark.asyncio
async def test_with_no_draft_or_link_left_no_tools(
    app, db, gateway, no_read_tools, links, set_env
):
    set_env("ASSISTANT_MAX_DRAFTS_PER_TURN", 0)
    llm = FakeLlm(calls({}, {}, {}, name="open_page"), says("Here."))
    equip(app, llm, gateway)
    await post_chat(app, "links").finish()
    assert [r.tool_choice for r in llm.requests] == ["auto", "none"]


# --- every call --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_thirteenth_call_does_not_run(app, db, gateway, reads):
    llm = FakeLlm(
        calls(*[{}] * 8, name="get_alerts"),
        calls(*[{}] * 5, name="get_alerts", first=9),
        says("That is all I can look at."),
    )
    equip(app, llm, gateway)
    before = counter(metrics.tool_calls_total, tool="get_alerts", outcome="tool_limit")
    turns = counter(metrics.turns_total, result="tool_limit")
    exchange = await post_chat(app, "everything").finish()
    assert reads.runs == 12
    assert outcomes(exchange) == ["found"] * 12 + ["tool_limit"]
    assert replies(llm.requests[2])[-1] == {"status": "error", "code": "tool_limit"}
    assert [r.tool_choice for r in llm.requests] == ["auto", "auto", "none"]
    assert exchange.event("done")["finish_reason"] == "tool_limit"
    assert (
        counter(metrics.tool_calls_total, tool="get_alerts", outcome="tool_limit")
        == before + 1
    )
    assert counter(metrics.turns_total, result="tool_limit") == turns + 1


@pytest.mark.asyncio
async def test_an_unknown_tool_counts_too(app, db, gateway, reads, set_env):
    set_env("ASSISTANT_MAX_TOOL_CALLS_PER_TURN", 2)
    llm = FakeLlm(
        calls({}, {}, name="delete_everything"),
        calls({}, name="get_alerts", first=3),
        says("No."),
    )
    equip(app, llm, gateway)
    exchange = await post_chat(app, "x").finish()
    assert reads.runs == 0
    assert [r.tool_choice for r in llm.requests] == ["auto", "none"]
    assert exchange.event("done")["finish_reason"] == "tool_limit"


@pytest.mark.asyncio
async def test_the_rounds_are_counted_and_seen(app, db, gateway, reads):
    observed = metrics.turn_tool_rounds._sum.get()
    llm = FakeLlm(
        calls({}, name="get_alerts"), calls({}, name="get_alerts", first=2), says("ok")
    )
    equip(app, llm, gateway)
    await post_chat(app, "x").finish()
    assert metrics.turn_tool_rounds._sum.get() == observed + 2


# --- time --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("seconds_left", "choice"), [(19.0, "none"), (21.0, "auto")])
async def test_no_tools_in_the_last_seconds(
    app, db, gateway, reads, monkeypatch, seconds_left, choice
):
    clock = FakeClock(time.monotonic())
    monkeypatch.setattr(chat_turn, "monotonic", clock)

    async def later():
        clock.advance(90 - seconds_left)

    llm = FakeLlm([later, *calls({}, name="get_alerts")], says("ok"))
    equip(app, llm, gateway)
    exchange = await post_chat(app, "x").finish()
    assert [r.tool_choice for r in llm.requests] == ["auto", choice]
    assert exchange.event("done")["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_the_turns_reads_end_before_its_deadline(
    app, db, gateway, reads, monkeypatch
):
    clock = FakeClock(time.monotonic())
    monkeypatch.setattr(chat_turn, "monotonic", clock)
    started = clock.now
    equip(app, FakeLlm(calls({}, name="get_alerts"), says("ok")), gateway)
    await post_chat(app, "x").finish()
    # The turn's own reader, and a draft's reader made from it.
    assert reads.deadlines == [started + 90 - 15] * 2
