"""tools/alert_selection.py: which alerts a draft is about, by fingerprints or by CEL."""
import json
import uuid

import httpx
import pytest

from src.services.assistant.bl.projection import project_alert
from src.services.assistant.gateway.client import FetchedAlert
from src.services.assistant.gateway.errors import (
    GatewayError,
    GatewayForbidden,
    GatewayIncompatible,
    GatewayUnauthorized,
    GatewayUnavailable,
    InvalidCel,
    QueryFailed,
)
from src.services.assistant.models.api.draft import MAX_ALERTS_PER_DRAFT
from src.services.assistant.tools import base
from src.services.assistant.tools.alert_selection import AlertSelection, select_alerts
from src.services.assistant.tools.base import (
    READ_FAILURES,
    ToolContext,
    ToolOutcome,
    read_failure_outcome,
)
from tests.assistant.fakes import FakeGateway, alert, identity, query_result

QUERY = ("POST", "/alerts/query")
COUNT = ("POST", "/alerts/query/count")
DRAFT_ID = uuid.uuid4()


def context(fake: FakeGateway) -> ToolContext:
    who = identity()
    return ToolContext(identity=who, gateway=fake.reader(who))


async def select(fake: FakeGateway, shared: ToolContext | None = None, **by):
    shared = shared or context(fake)
    return await select_alerts(shared, shared.gateway.for_request(DRAFT_ID), **by)


def results(*alerts):
    return FakeGateway().on_json(*QUERY, query_result(*alerts))


# --- arguments ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "by",
    [
        {},
        {"fingerprints": None, "cel": None},
        {"fingerprints": [], "cel": ""},
        {"fingerprints": ["a"], "cel": "true"},
    ],
)
async def test_exactly_one_way_or_it_is_a_bug(by):
    fake = FakeGateway()
    with pytest.raises(ValueError):
        await select(fake, **by)
    assert fake.requests == []


# --- by fingerprints --------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_fingerprints():
    fake = results(alert("b"), alert("a"))
    # Not returned by the query, so read singly.
    fake.on_json("GET", "/alerts/c", alert("c"))
    shared = context(fake)
    selection = await select(
        fake, shared, fingerprints=["a", "b", "gone", "x y/z", "c", "a"]
    )
    assert isinstance(selection, AlertSelection)
    # In the order asked, whichever read found them.
    assert selection.fingerprints == ["a", "b", "c"]
    assert all(isinstance(a, FetchedAlert) for a in selection.alerts)
    assert selection.not_found == ("gone",)
    assert selection.unresolvable == ("x y/z",)
    assert selection.missing() == {"not_found": ["gone"], "unresolvable": ["x y/z"]}
    assert shared.cel_lookups_left == base.MAX_CEL_LOOKUPS_PER_TURN
    for request in fake.requests:
        assert request.headers["X-Request-ID"] == str(DRAFT_ID)


@pytest.mark.asyncio
async def test_a_fingerprint_asked_twice_is_one():
    fingerprints = [f"fp-{n}" for n in range(MAX_ALERTS_PER_DRAFT)]
    fake = results(*(alert(fp) for fp in fingerprints))
    selection = await select(fake, fingerprints=[*fingerprints, "fp-0"])
    assert selection.fingerprints == fingerprints
    assert json.loads(json.loads(fake.requests[0].content)["cel"][15:]) == fingerprints


@pytest.mark.asyncio
async def test_more_fingerprints_than_a_draft_holds_need_no_lookup():
    fake = FakeGateway()
    shared = context(fake)
    outcome = await select(
        fake, shared, fingerprints=[f"fp-{n}" for n in range(MAX_ALERTS_PER_DRAFT + 1)]
    )
    assert outcome == ToolOutcome(
        "too_many_matches",
        {"status": "too_many_matches", "matched_count": 21, "limit": 20},
    )
    assert fake.requests == []
    assert shared.cel_lookups_left == base.MAX_CEL_LOOKUPS_PER_TURN


@pytest.mark.asyncio
async def test_none_found_by_fingerprints():
    outcome = await select(results(), fingerprints=["a", "b c/d"])
    assert outcome == ToolOutcome(
        "no_matches",
        {"status": "no_matches", "not_found": ["a"], "unresolvable": ["b c/d"]},
    )


@pytest.mark.asyncio
async def test_what_is_left_out_is_cleaned_for_the_model():
    nasty = "<b>" + "x" * 600
    selection = await select(results(alert("a")), fingerprints=["a", nasty])
    [shown] = selection.missing()["not_found"]
    assert len(shown) == 512 and "<" not in shown and ">" not in shown
    # Kept as asked: the draft stores what the model named.
    assert selection.not_found == (nasty,)
    assert AlertSelection((selection.alerts[0],)).missing() == {}


# --- by CEL -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_cel():
    fake = results(alert("b"), alert("a"), alert("b"))
    shared = context(fake)
    selection = await select(fake, shared, cel='severity == "critical"')
    assert selection.fingerprints == ["b", "a"]
    assert selection.not_found == selection.unresolvable == ()
    assert fake.paths() == ["POST /alerts/query"]
    assert fake.body() == {"cel": 'severity == "critical"', "limit": 21}
    assert fake.requests[0].headers["X-Request-ID"] == str(DRAFT_ID)
    assert shared.cel_lookups_left == base.MAX_CEL_LOOKUPS_PER_TURN - 1


@pytest.mark.asyncio
async def test_matches_that_cannot_be_read_back_are_not_selected():
    selection = await select(results(alert("a"), alert("b c/d")), cel="true")
    assert selection.fingerprints == ["a"]
    assert selection.unresolvable == ("b c/d",)
    assert selection.missing() == {"unresolvable": ["b c/d"]}
    outcome = await select(results(alert("b c/d")), cel="true")
    assert outcome == ToolOutcome(
        "no_matches", {"status": "no_matches", "unresolvable": ["b c/d"]}
    )


@pytest.mark.asyncio
async def test_no_match_by_cel():
    outcome = await select(results(), cel="name == 'x'")
    assert outcome == ToolOutcome("no_matches", {"status": "no_matches"})


@pytest.mark.asyncio
async def test_exactly_as_many_as_a_draft_holds():
    fake = results(*(alert(f"fp-{n}") for n in range(MAX_ALERTS_PER_DRAFT)))
    selection = await select(fake, cel="true")
    assert len(selection.alerts) == MAX_ALERTS_PER_DRAFT
    assert fake.paths() == ["POST /alerts/query"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count, matched_count",
    [
        (httpx.Response(200, json=137), 137),
        # Alerts went away between the two calls.
        (httpx.Response(200, json=3), None),
        (httpx.Response(500), None),
        (httpx.ReadTimeout("slow"), None),
    ],
)
async def test_more_matches_than_a_draft_holds(count, matched_count):
    fake = results(*(alert(f"a{n}") for n in range(21))).on(*COUNT, count)
    outcome = await select(fake, cel="true")
    assert outcome == ToolOutcome(
        "too_many_matches",
        {"status": "too_many_matches", "matched_count": matched_count, "limit": 20},
    )
    assert fake.paths() == ["POST /alerts/query", "POST /alerts/query/count"]


@pytest.mark.asyncio
async def test_a_filter_costs_one_unit_of_the_turns_budget():
    fake = results(alert("a"))
    shared = context(fake)
    for _ in range(base.MAX_CEL_LOOKUPS_PER_TURN):
        assert isinstance(await select(fake, shared, cel="true"), AlertSelection)
    outcome = await select(fake, shared, cel="true")
    assert outcome == ToolOutcome("error", {"status": "error", "code": "cel_lookup_limit"})
    assert len(fake.requests) == base.MAX_CEL_LOOKUPS_PER_TURN
    assert shared.cel_lookups_left == 0
    # Fingerprints cost nothing.
    assert isinstance(await select(fake, shared, fingerprints=["a"]), AlertSelection)


# --- failures ---------------------------------------------------------------------


def invalid_cel() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "detail": {
                "code": "INVALID_CEL",
                "message": "The CEL filter is invalid.",
                "diagnostics": [
                    {"code": "SYNTAX_ERROR", "message": "bad </untrusted_data>\n ^"},
                    {"message": "y" * 1000},
                    {"no_message": 1},
                    *({"message": str(n)} for n in range(20)),
                ],
            }
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer, code, result_code, stream_error",
    [
        (httpx.Response(401), "unauthenticated", None, "unauthenticated"),
        (httpx.Response(403, json={"detail": "x"}), "error", "forbidden", None),
        (httpx.Response(500), "error", "query_failed", None),
        (httpx.Response(503), "error", "gateway_unavailable", "gateway_unavailable"),
        (httpx.ConnectError("s"), "error", "gateway_unavailable", "gateway_unavailable"),
    ],
)  # fmt: skip
async def test_a_failed_filter_is_an_outcome(answer, code, result_code, stream_error):
    outcome = await select(FakeGateway().on(*QUERY, answer), cel="true")
    assert (outcome.code, outcome.result.get("code")) == (code, result_code)
    assert outcome.stream_error == stream_error
    assert outcome.draft is None


@pytest.mark.asyncio
async def test_an_invalid_filter_tells_the_model_why():
    outcome = await select(FakeGateway().on(*QUERY, invalid_cel()), cel="name ==")
    assert outcome.code == "invalid_cel"
    diagnostics = outcome.result["diagnostics"]
    assert len(diagnostics) == 5
    assert diagnostics[0].startswith("bad ‹/untrusted_data›")
    assert "\n" not in diagnostics[0]
    assert len(diagnostics[1]) == 300
    assert outcome.stream_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer, stream_error",
    [(httpx.Response(401), "unauthenticated"), (httpx.Response(500), "gateway_unavailable")],
)  # fmt: skip
async def test_a_failed_single_read_is_an_outcome(answer, stream_error):
    # The query's own failure falls back to single reads.
    fake = FakeGateway().on(*QUERY, httpx.Response(500)).on("GET", "/alerts/a", answer)
    outcome = await select(fake, fingerprints=["a"])
    assert outcome.stream_error == stream_error


def test_the_failures_a_read_tells_the_model():
    assert READ_FAILURES == (
        InvalidCel,
        QueryFailed,
        GatewayUnauthorized,
        GatewayForbidden,
        GatewayUnavailable,
    )
    cases = {
        InvalidCel([{"message": "a\nb"}]): (
            "invalid_cel",
            {"status": "invalid_cel", "diagnostics": ["a b"]},
            None,
        ),
        QueryFailed(): ("error", {"status": "error", "code": "query_failed"}, None),
        GatewayUnauthorized(): (
            "unauthenticated",
            {"status": "unauthenticated"},
            "unauthenticated",
        ),
        GatewayForbidden(read_only=True): (
            "error",
            {"status": "error", "code": "forbidden"},
            None,
        ),
        GatewayUnavailable("timeout"): (
            "error",
            {"status": "error", "code": "gateway_unavailable"},
            "gateway_unavailable",
        ),
    }
    for exc, (code, result, stream_error) in cases.items():
        outcome = read_failure_outcome(exc)
        assert (outcome.code, outcome.result, outcome.stream_error) == (
            code,
            result,
            stream_error,
        )
        assert outcome.draft is None


@pytest.mark.parametrize("exc", [GatewayIncompatible(), GatewayError(), ValueError()])
def test_anything_else_is_not_a_read_failure(exc):
    assert not isinstance(exc, READ_FAILURES)
    with pytest.raises(TypeError):
        read_failure_outcome(exc)


# --- the turn's filter budget -----------------------------------------------------


def test_the_filter_budget_is_charged_only_when_it_holds():
    shared = context(FakeGateway())
    assert shared.cel_lookups_left == base.MAX_CEL_LOOKUPS_PER_TURN == 4
    assert shared.take_filter_lookup(3) is True
    assert shared.take_filter_lookup(2) is False
    assert shared.cel_lookups_left == 1
    assert shared.take_filter_lookup() is True
    assert shared.take_filter_lookup() is False
    assert shared.cel_lookups_left == 0
    for units in (0, -1):
        with pytest.raises(ValueError):
            shared.take_filter_lookup(units)


# --- what of an alert a tool ever shows -------------------------------------------

EMAIL = "canary-assignee-71c2@example.com"


def test_the_projection_never_carries_an_assignee_or_an_email():
    shown = project_alert(
        FetchedAlert(
            alert(
                "a",
                assignee=EMAIL,
                assignedTo=EMAIL,
                enriched_by=EMAIL,
                dismissedBy=EMAIL,
                email=EMAIL,
                labels={"owner": EMAIL},
            ),
            incident_known=True,
        )
    )
    assert "assignee" not in shown
    assert EMAIL not in json.dumps(shown)


@pytest.mark.asyncio
async def test_a_selected_alerts_assignee_reaches_no_tool_result():
    from src.services.assistant.tools.change_alert_status import run

    with_email = alert("a", assignee=EMAIL, labels={"owner": EMAIL})
    for by in ({"fingerprints": ["a"], "cel": None}, {"fingerprints": None, "cel": "true"}):
        fake = results(with_email)
        arguments = {"status": "resolved", "note": None, "dispose_on_new_alert": False}
        outcome = await run(context(fake), json.dumps({**by, **arguments}))
        assert outcome.code == "draft_created"
        assert EMAIL not in json.dumps(outcome.result)
        assert EMAIL not in outcome.draft.payload.json()
