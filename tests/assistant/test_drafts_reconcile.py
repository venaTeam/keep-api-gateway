"""A draft whose Confirm never saw the result gets it when somebody looks."""
import asyncio
import json
import logging

import httpx
import pytest

from src.services.assistant.bl import drafts_bl, reconcile_bl
from src.services.assistant.core import metrics
from src.services.assistant.kinds import alert_set
from src.services.assistant.models.db import Conversation, Draft
from tests.assistant.logs import rendered
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    add_message,
    ago,
    change,
    messages,
    read,
)
from tests.assistant.running import Exchange, Keep, equip, post_confirm, until

CANARY = "canary-90be2a"
KIND = {"kind": "change_alert_status"}


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def keep(app):
    keep = Keep({"fp-1": "firing", "fp-2": "firing"})
    equip(app, gateway=keep)
    return keep


@pytest.fixture()
def conversation(db):
    return add_conversation(db, updated_at=ago(days=3))


def pending_outcome(gateway_result="unknown", **results: str) -> dict:
    """As Confirm leaves it behind."""
    return {
        "code": "outcome_pending",
        "message": "Keep is still applying this change.",
        "gateway_result": gateway_result,
        "applied_count": 0,
        "failed_count": 0,
        "unconfirmed_count": 2,
        "skipped_count": 0,
        "not_found_count": 0,
        "results": [
            {
                "fingerprint": fp.replace("_", "-"),
                "name": None,
                "result": result,
                "status_after": "acknowledged" if result == "skipped" else None,
            }
            for fp, result in results.items()
        ],
        "finished_at": None,
    }


def applying(db, conversation, *fingerprints, seconds=31, **values):
    draft = add_draft(
        db,
        conversation,
        *fingerprints,
        **{"state": "applying", "updated_at": ago(seconds=seconds), **values},
    )
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    return draft


async def look(app, conversation) -> list[dict]:
    """The drafts of the conversation."""
    exchange = await Exchange(
        app, "GET", f"/assistant/conversations/{conversation}"
    ).finish()
    assert exchange.status == 200, exchange.text
    return [i["draft"] for i in exchange.json()["items"] if i["type"] == "draft"]


def by_alert(draft: dict) -> dict:
    return {
        r["fingerprint"]: (r["result"], r["status_after"])
        for r in draft["outcome"]["results"]
    }


def counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def finalized() -> float:
    return counter(metrics.drafts_total, event="finalized_after_timeout", **KIND)


def ended(outcome: str) -> float:
    return counter(metrics.draft_outcomes_total, outcome=outcome, **KIND)


# --- what the alerts say ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, pending_outcome()], ids=["died", "timeout"])
@pytest.mark.parametrize(
    ("statuses", "state", "code", "results"),
    [
        (
            {"fp-1": "acknowledged", "fp-2": "acknowledged"},
            "applied",
            "applied",
            {"fp-1": ("applied", "acknowledged"), "fp-2": ("applied", "acknowledged")},
        ),
        (
            {"fp-1": "acknowledged", "fp-2": "firing"},
            "partially_applied",
            "partially_applied",
            {"fp-1": ("applied", "acknowledged"), "fp-2": ("failed", "firing")},
        ),
        (
            {"fp-1": "acknowledged"},
            "partially_applied",
            "partially_applied",
            {"fp-1": ("applied", "acknowledged"), "fp-2": ("unconfirmed", None)},
        ),
        (
            {"fp-1": "firing", "fp-2": "resolved"},
            "failed",
            "interrupted",
            {"fp-1": ("failed", "firing"), "fp-2": ("failed", "resolved")},
        ),
        (
            {"fp-1": "firing"},
            "failed",
            "interrupted",
            {"fp-1": ("failed", "firing"), "fp-2": ("unconfirmed", None)},
        ),
        (
            {},
            "failed",
            "outcome_unknown",
            {"fp-1": ("unconfirmed", None), "fp-2": ("unconfirmed", None)},
        ),
    ],
)
async def test_the_result(
    app, db, keep, conversation, stored, statuses, state, code, results
):
    keep.statuses = statuses
    draft = applying(db, conversation, "fp-1", "fp-2", outcome=stored)
    before = read(db, Conversation, conversation)
    count = finalized()
    [view] = await look(app, conversation)
    assert (view["state"], view["outcome"]["code"]) == (state, code)
    assert by_alert(view) == results
    assert view["outcome"]["gateway_result"] == "unknown"
    assert view["outcome"]["finished_at"] is not None
    assert view["resolved_at"] is not None
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]["code"]) == (state, code)
    assert keep.writes == []
    assert finalized() == count + 1
    # The user only looked.
    assert read(db, Conversation, conversation) == before
    assert len(keep.looks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["http_error", "failed_response"])
async def test_what_the_gateway_said_is_kept(app, db, keep, conversation, result):
    applying(db, conversation, "fp-1", outcome=pending_outcome(result))
    [view] = await look(app, conversation)
    assert view["outcome"]["gateway_result"] == result


@pytest.mark.asyncio
async def test_only_what_was_written_is_looked_at(app, db, keep, conversation):
    """The others have their result already."""
    keep.statuses = {"fp-1": "acknowledged", "fp-2": "firing", "fp-3": "firing"}
    applying(
        db,
        conversation,
        "fp-1",
        "fp-2",
        "fp-3",
        outcome=pending_outcome(fp_1="unconfirmed", fp_2="skipped", fp_3="not_found"),
    )
    [view] = await look(app, conversation)
    assert json.loads(keep.looks[0].content)["cel"] == 'fingerprint in ["fp-1"]'
    assert view["state"] == "applied"
    assert by_alert(view) == {
        "fp-1": ("applied", "acknowledged"),
        "fp-2": ("skipped", "acknowledged"),
        "fp-3": ("not_found", None),
    }


@pytest.mark.asyncio
async def test_the_model_is_told(app, db, keep, conversation):
    draft = applying(db, conversation, "fp-1", "fp-2")
    await look(app, conversation)
    event = messages(db, conversation)[-1]
    assert (event["role"], event["draft_id"]) == ("event", draft)
    assert event["content"] == (
        f"draft {draft} (change_alert_status to acknowledged): interrupted. "
        "0 applied, 2 failed, 0 unconfirmed, 0 skipped, 0 not found."
    )
    await look(app, conversation)
    assert len(messages(db, conversation)) == 2


@pytest.mark.asyncio
async def test_after_a_confirm_that_timed_out(app, db, keep, conversation, set_env):
    """The whole way."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 0.1)
    draft = add_draft(db, conversation, "fp-1", "fp-2")
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    write = keep._write

    async def slow(request):
        await asyncio.sleep(0.6)
        return await write(request)

    keep.on("POST", "/alerts/batch_enrich", slow)
    confirmed = await post_confirm(app, draft).finish()
    assert confirmed.json()["outcome"]["code"] == "outcome_pending"
    [view] = await look(app, conversation)
    assert view["state"] == "applying"
    await until(lambda: keep.statuses["fp-2"] == "acknowledged")
    change(db, Draft, draft, updated_at=ago(seconds=31))
    [view] = await look(app, conversation)
    assert (view["state"], view["outcome"]["applied_count"]) == ("applied", 2)


# --- when -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_while_a_confirm_may_still_be_at_it(app, db, keep, conversation):
    draft = applying(db, conversation, seconds=25)
    before = read(db, Draft, draft)
    [view] = await look(app, conversation)
    assert view["state"] == "applying"
    assert keep.looks == []
    assert read(db, Draft, draft) == before


@pytest.mark.asyncio
async def test_one_attempt_in_thirty_seconds(app, db, keep, conversation):
    keep.on("POST", "/alerts/query", httpx.Response(502))
    draft = applying(db, conversation, outcome=pending_outcome())
    for _ in range(3):
        [view] = await look(app, conversation)
        assert view["state"] == "applying"
        assert view["outcome"]["code"] == "outcome_pending"
    assert len(keep.looks) == 1
    change(db, Draft, draft, updated_at=ago(seconds=31))
    keep.on("POST", "/alerts/query", keep._query)
    [view] = await look(app, conversation)
    assert view["outcome"]["code"] == "interrupted"


@pytest.mark.asyncio
async def test_two_who_look_at_once(app, db, keep, conversation):
    applying(db, conversation)
    released = asyncio.Event()

    async def slow(request):
        await released.wait()
        return await Keep._query(keep, request)

    keep.on("POST", "/alerts/query", slow)
    count = finalized()
    both = [asyncio.create_task(look(app, conversation)) for _ in range(2)]
    await until(lambda: any(task.done() for task in both))
    released.set()
    await asyncio.gather(*both)
    assert len(keep.looks) == 1
    assert finalized() == count + 1
    assert [m["role"] for m in messages(db, conversation)] == ["tool", "event"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["pending", "applied", "partially_applied", "failed", "discarded", "expired"],
)
async def test_only_drafts_that_are_being_applied(app, db, keep, conversation, state):
    draft = applying(db, conversation, state=state, seconds=3600)
    before = read(db, Draft, draft)
    await look(app, conversation)
    assert keep.looks == []
    assert read(db, Draft, draft) == before


@pytest.mark.asyncio
async def test_one_that_gets_its_result_meanwhile(
    app, db, keep, conversation, monkeypatch
):
    """Between reading the conversation and looking at its alerts."""
    draft = applying(db, conversation)
    claim = drafts_bl.claim_look
    settled = {}

    def after_the_result(*args):
        change(db, Draft, draft, state="applied")
        settled.update(read(db, Draft, draft))
        return claim(*args)

    monkeypatch.setattr(drafts_bl, "claim_look", after_the_result)
    await look(app, conversation)
    assert keep.looks == []
    assert read(db, Draft, draft) == settled


@pytest.mark.asyncio
async def test_one_that_gets_its_result_during_the_look(app, db, keep, conversation):
    """That result stays, is shown, and is counted by who gave it."""
    draft = applying(db, conversation)

    async def meanwhile(request):
        change(db, Draft, draft, state="applied")
        return await Keep._query(keep, request)

    keep.on("POST", "/alerts/query", meanwhile)
    counts = finalized(), ended("interrupted")
    [view] = await look(app, conversation)
    assert (view["state"], view["outcome"]) == ("applied", None)
    assert (finalized(), ended("interrupted")) == counts
    assert [m["role"] for m in messages(db, conversation)] == ["tool"]


@pytest.mark.asyncio
async def test_the_result_is_counted(app, db, keep, conversation):
    counts = finalized(), ended("interrupted")
    applying(db, conversation)
    await look(app, conversation)
    assert (finalized(), ended("interrupted")) == (counts[0] + 1, counts[1] + 1)


@pytest.mark.asyncio
async def test_several_drafts_at_once(app, db, keep, conversation):
    async def slow(request):
        await asyncio.sleep(0.5)
        return await Keep._query(keep, request)

    keep.on("POST", "/alerts/query", slow)
    for _ in range(3):
        applying(db, conversation)
    started = asyncio.get_running_loop().time()
    views = await look(app, conversation)
    assert asyncio.get_running_loop().time() - started < 1.4
    assert {v["outcome"]["code"] for v in views} == {"interrupted"}


@pytest.mark.asyncio
async def test_it_does_not_hold_the_conversation_up_for_long(
    app, db, keep, conversation, monkeypatch
):
    assert (reconcile_bl.AFTER_SECONDS, reconcile_bl.READ_SECONDS) == (30, 4)
    monkeypatch.setattr(reconcile_bl, "READ_SECONDS", 0.2)

    async def slow(request):
        await asyncio.sleep(3)

    keep.on("POST", "/alerts/query", slow)
    applying(db, conversation)
    started = asyncio.get_running_loop().time()
    [view] = await look(app, conversation)
    assert asyncio.get_running_loop().time() - started < 2
    assert view["state"] == "applying"


# --- failures --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bug_does_not_hide_the_conversation(
    app, db, keep, conversation, monkeypatch, caplog
):
    def fail(*args, **options):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(alert_set, "verified", fail)
    applying(db, conversation)
    with caplog.at_level(logging.DEBUG):
        [view] = await look(app, conversation)
    assert view["state"] == "applying"
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert [r for r in caplog.records if r.message == "assistant: draft not reconciled"]


@pytest.mark.asyncio
async def test_a_kind_this_version_does_not_know(app, db, keep, conversation):
    applying(db, conversation, kind="from_the_future")
    [view] = await look(app, conversation)
    assert view["state"] == "applying"
    assert keep.looks == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unreadable",
    [
        {"kind": "from_the_future"},
        {"payload": {"kind": CANARY}},
        {"outcome": pending_outcome(gateway_result=CANARY)},
    ],
    ids=["kind", "payload", "outcome"],
)
async def test_what_this_version_cannot_read_waits_for_the_sweep(
    app, db, keep, conversation, caplog, unreadable
):
    """Never closed here, and never claimed: looking again and again neither floods
    the log nor keeps the 24 h sweep away."""
    draft = applying(db, conversation, **unreadable)
    before = read(db, Draft, draft)
    errors = metrics.internal_errors_total.labels(site="reconcile")
    count = errors._value.get()
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            [view] = await look(app, conversation)
            assert view["state"] == "applying"
    assert read(db, Draft, draft) == before
    assert errors._value.get() == count
    assert keep.looks == []
    assert not [
        r
        for r in caplog.records
        if r.name.startswith("src.services.assistant") and r.levelno >= logging.WARNING
    ]
    assert not [r for r in caplog.records if CANARY in rendered(r)]


@pytest.mark.asyncio
async def test_a_draft_of_someone_else_is_not_touched(app, db, keep):
    theirs = add_conversation(db, created_by="else@example.com")
    draft = applying(db, theirs, created_by="else@example.com")
    before = read(db, Draft, draft)
    exchange = await Exchange(app, "GET", f"/assistant/conversations/{theirs}").finish()
    assert exchange.status == 404
    assert read(db, Draft, draft) == before
    assert keep.looks == []
