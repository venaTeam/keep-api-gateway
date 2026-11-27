"""Confirm's checkpoints and its `confirm_state`: stored before each write, given to
whoever finishes the draft, never shown, and what each write of a draft does with it.
Every timing of Confirm here is a fake clock's."""

import ast
import asyncio
import inspect
import json
import textwrap
import threading
from datetime import datetime, timezone

import httpx
import pytest

from src.services.assistant.bl import confirm_bl, drafts_bl, sweeps
from src.services.assistant.bl.drafts_bl import DraftKey
from src.services.assistant.kinds.base import MAX_CONFIRM_STATE_BYTES
from src.services.assistant.models.db import Draft
from tests.assistant.conftest import TENANT
from tests.assistant.fakes import identity
from tests.assistant.rows import (
    EMAIL,
    add_conversation,
    add_draft,
    add_message,
    ago,
    change,
    read,
)
from tests.assistant.running import Keep, equip, post_confirm, use_fake_clock
from tests.assistant.test_confirm_plan import (
    FINGERPRINTS,
    NAME,
    OBJECTS,
    Writes,
    look,
    on_write,
    outcome,
    register,
)

STATE = {"step": 0, "sent_at": "2026-11-27T10:00:00+00:00", "object_ids": []}
LATER = {"step": 1, "sent_at": "2026-11-27T10:00:05+00:00", "object_ids": [None]}


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def keep():
    return Keep({"fp-1": "firing", "fp-2": "firing"})


@pytest.fixture()
def work(app, keep):
    return equip(app, gateway=keep)


@pytest.fixture()
def conversation(db):
    return add_conversation(db, updated_at=ago(days=3))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    return use_fake_clock(monkeypatch)


@pytest.fixture()
def kind(monkeypatch):
    return register(monkeypatch, Writes())


@pytest.fixture()
def draft(db, work, conversation, kind):
    """Of two writes, one per fingerprint."""
    return add_draft(db, conversation, *FINGERPRINTS, kind=NAME)


@pytest.fixture()
def alert_draft(db, work, conversation):
    """Of one write."""
    return add_draft(db, conversation, *FINGERPRINTS)


def key(draft, conversation) -> DraftKey:
    return DraftKey(draft, TENANT, EMAIL, conversation)


def so_far(code="partially_applied", made=OBJECTS[0]) -> dict:
    return json.loads(outcome(code, "ok", made).json())


SO_FAR = so_far()


def as_stored(value) -> dict:
    """As a draft stores it: without an object, no `object` key."""
    stored = json.loads(value.json())
    if stored["object"] is None:
        del stored["object"]
    return stored


def without_state(stored: dict) -> dict:
    return {k: v for k, v in stored.items() if k != "confirm_state"}


# --- the order: checkpoint, checks, send ----------------------------------------------


def test_nothing_is_awaited_between_the_checks_and_the_write():
    """The send is the one await of `_send`, after the drain and the time check."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(confirm_bl._Attempt._send)))
    waits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor))
    ]
    [send] = waits
    assert isinstance(send, ast.Await) and isinstance(send.value, ast.Call)
    assert ast.unparse(send.value.func) == "self._gateway.write"
    checks = {
        ast.unparse(node.test): node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
    }
    assert checks.keys() >= {"self._work.draining", "self._seconds_left() < needed"}
    assert all(line < send.lineno for line in checks.values())


@pytest.mark.asyncio
async def test_the_checkpoint_comes_before_the_first_write(
    app, db, keep, alert_draft
):
    """It keeps `updated_at`: that says when somebody may look at the alerts again."""
    seen = {}

    async def a_look(request):
        seen.setdefault("claimed", read(db, Draft, alert_draft))
        return await keep._query(request)

    async def write(request):
        seen["written"] = read(db, Draft, alert_draft)
        return await keep._write(request)

    keep.on("POST", "/alerts/query", a_look)
    keep.on("POST", "/alerts/batch_enrich", write)
    before = datetime.now(timezone.utc)
    exchange = await post_confirm(app, alert_draft).finish()
    assert exchange.json()["state"] == "applied"
    claimed, written = seen["claimed"], seen["written"]
    assert (claimed["state"], claimed["outcome"]) == ("applying", None)
    assert written["state"] == "applying"
    assert written["outcome"].keys() == {"confirm_state"}
    state = written["outcome"]["confirm_state"]
    assert (state["step"], state["object_ids"]) == (0, [])
    sent_at = datetime.fromisoformat(state["sent_at"])
    assert before <= sent_at <= datetime.now(timezone.utc)
    assert written["updated_at"] == claimed["updated_at"]
    # The result keeps it.
    assert read(db, Draft, alert_draft)["outcome"]["confirm_state"] == state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "answer"),
    [("drain", (503, "not_ready")), ("time", (502, "gateway_unavailable"))],
)
async def test_a_refusal_after_the_checkpoint_puts_the_draft_back_without_it(
    app, db, keep, work, clock, alert_draft, monkeypatch, refusal, answer
):
    """The checks run after the checkpoint: what happens during it counts."""
    stored = drafts_bl.checkpoint

    def then_refused(*args, **kwargs):
        done = stored(*args, **kwargs)
        if refusal == "drain":
            work.drain()
        else:
            clock.advance(confirm_bl.BUDGET_SECONDS)
        return done

    monkeypatch.setattr(drafts_bl, "checkpoint", then_refused)
    exchange = await post_confirm(app, alert_draft).finish(timeout=5)
    assert (exchange.status, exchange.code) == answer
    row = read(db, Draft, alert_draft)
    assert (row["state"], row["outcome"]) == ("pending", None)
    assert keep.writes == []


@pytest.mark.asyncio
async def test_a_draft_settled_before_the_first_checkpoint_gets_no_write(
    app, db, keep, alert_draft
):
    async def settled(request):
        change(db, Draft, alert_draft, state="failed")
        return await keep._query(request)

    keep.on("POST", "/alerts/query", settled)
    exchange = await post_confirm(app, alert_draft).finish()
    assert exchange.status == 200, exchange.text
    assert exchange.json()["state"] == "failed"
    assert keep.writes == []
    assert read(db, Draft, alert_draft)["outcome"] is None


@pytest.mark.asyncio
async def test_a_checkpoint_after_the_first_write_that_finds_no_row(
    app, db, keep, conversation, draft, set_env
):
    """Settled meanwhile: write 2 is not sent, and what is stored stays."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)

    async def then_settled(request):
        answer = await keep._write(request)
        change(db, Draft, draft, state="failed")
        return answer

    on_write(keep, then_settled)
    exchange = await post_confirm(app, draft).finish()
    assert exchange.status == 200, exchange.text
    assert exchange.json()["state"] == "failed"
    assert len(keep.writes) == 1
    stored = read(db, Draft, draft)["outcome"]
    assert stored.keys() == {"confirm_state"}
    assert stored["confirm_state"]["step"] == 0


@pytest.mark.asyncio
async def test_a_checkpoint_after_a_write_stores_what_it_made(
    app, db, keep, draft, set_env
):
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    seen = {}

    async def second(request):
        seen["row"] = read(db, Draft, draft)
        return await keep._write(request)

    on_write(keep, keep._write, second)
    exchange = await post_confirm(app, draft).finish()
    assert exchange.json()["state"] == "applied"
    row = seen["row"]
    assert row["state"] == "applying"
    assert row["outcome"]["code"] == "partially_applied"
    assert row["outcome"]["object"]["id"] == OBJECTS[0].id
    state = row["outcome"]["confirm_state"]
    assert (state["step"], state["object_ids"]) == (1, [None])


# --- never shown ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_is_never_shown(app, db, keep, conversation, alert_draft):
    """Not in Confirm's answer, nor in the conversation: only in the stored row."""

    async def written_but_unanswered(request):
        await keep._write(request)
        return httpx.Response(502)

    keep.on("POST", "/alerts/batch_enrich", written_but_unanswered)
    exchange = await post_confirm(app, alert_draft).finish()
    assert exchange.status == 200, exchange.text
    body = exchange.json()
    assert (body["state"], body["outcome"]["code"]) == ("applying", "outcome_pending")
    assert "confirm_state" not in exchange.text
    assert read(db, Draft, alert_draft)["outcome"]["confirm_state"]["step"] == 0
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=alert_draft)
    viewed = await look(app, conversation)
    assert viewed["outcome"]["code"] == "outcome_pending"
    assert "confirm_state" not in json.dumps(viewed)


@pytest.mark.asyncio
async def test_a_draft_with_nothing_but_it_shows_no_outcome(app, db, conversation):
    equip(app)
    draft = add_draft(
        db, conversation, state="applying", outcome={"confirm_state": STATE}
    )
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    viewed = await look(app, conversation)
    assert (viewed["state"], viewed["outcome"]) == ("applying", None)
    assert drafts_bl.load(key(draft, conversation)).outcome is None


# --- the limit ------------------------------------------------------------------------


def test_the_limit_is_4_kib():
    assert MAX_CONFIRM_STATE_BYTES == 4096


def test_a_state_over_the_limit_is_not_stored(db, conversation):
    draft = add_draft(db, conversation, state="applying")
    padding = MAX_CONFIRM_STATE_BYTES - len('{"pad":""}')
    assert drafts_bl.checkpoint(key(draft, conversation), {"pad": "x" * padding})
    before = read(db, Draft, draft)
    with pytest.raises(ValueError):
        drafts_bl.checkpoint(key(draft, conversation), {"pad": "x" * (padding + 1)})
    # In UTF-8: a character of two bytes counts twice.
    with pytest.raises(ValueError):
        drafts_bl.checkpoint(key(draft, conversation), {"pad": "é" * padding})
    assert read(db, Draft, draft) == before


@pytest.mark.asyncio
async def test_a_state_over_the_limit_is_a_bug_and_nothing_is_sent(
    app, db, keep, alert_draft, monkeypatch
):
    monkeypatch.setattr(drafts_bl, "MAX_CONFIRM_STATE_BYTES", 16)
    exchange = await post_confirm(app, alert_draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    row = read(db, Draft, alert_draft)
    assert (row["state"], row["outcome"]) == ("pending", None)
    assert keep.writes == []


# --- whoever finishes the draft -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_attempt_cancelled_during_the_second_write(
    app, db, keep, work, kind, conversation, draft, set_env
):
    """What the first write made is stored, and reconcile finds it."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    reached, released = asyncio.Event(), asyncio.Event()

    async def held(request):
        reached.set()
        await released.wait()
        return await keep._write(request)

    on_write(keep, keep._write, held)
    exchange = post_confirm(app, draft)
    await asyncio.wait_for(reached.wait(), 5)
    [attempt] = work._tasks
    attempt.cancel()
    exchange.task.cancel()
    await asyncio.wait({attempt, exchange.task}, timeout=5)
    assert attempt.cancelled()
    released.set()
    assert await app.state.assistant_gateway.wait_for_writes(5) == 0
    row = read(db, Draft, draft)
    assert row["state"] == "applying"
    assert row["outcome"]["object"]["id"] == OBJECTS[0].id
    assert row["outcome"]["confirm_state"]["step"] == 1
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    change(db, Draft, draft, updated_at=ago(seconds=31))
    viewed = await look(app, conversation)
    assert (viewed["state"], viewed["outcome"]["object"]["id"]) == (
        "applied",
        OBJECTS[1].id,
    )
    [(view, confirm_state)] = kind.reconciled
    assert view.outcome.object.id == OBJECTS[0].id
    assert (confirm_state["step"], confirm_state["object_ids"]) == (1, [None])
    assert read(db, Draft, draft)["outcome"]["confirm_state"] == confirm_state


class Closing(Writes):
    def __init__(self):
        super().__init__()
        self.closed: list = []

    def close_unknown(self, payload, stored, confirm_state):
        self.closed.append((stored, confirm_state))
        return super().close_unknown(payload, stored, confirm_state)


@pytest.mark.parametrize(
    ("stored", "given"),
    [
        ({"confirm_state": LATER}, LATER),
        ({**SO_FAR, "confirm_state": LATER}, LATER),
        ({**SO_FAR, "confirm_state": "not a state"}, None),
        (SO_FAR, None),
    ],
    ids=["only-the-state", "with-an-outcome", "not-an-object", "none"],
)
def test_the_sweep_gives_it_to_close_unknown(
    db, conversation, monkeypatch, stored, given
):
    kind = register(monkeypatch, Closing())
    draft = add_draft(
        db,
        conversation,
        *FINGERPRINTS,
        kind=NAME,
        state="applying",
        updated_at=ago(hours=25),
        outcome=stored,
    )
    sweeps.run(threading.Event())
    [(shown, confirm_state)] = kind.closed
    assert confirm_state == given
    if without_state(stored):
        assert shown.object.id == OBJECTS[0].id
    else:
        assert shown is None
    row = read(db, Draft, draft)
    assert row["state"] == "failed"
    assert row["outcome"].get("confirm_state") == stored.get("confirm_state")


# --- what each write does with it -----------------------------------------------------


def test_the_claim_and_a_discard_store_none(db, conversation):
    draft = add_draft(db, conversation)
    who = identity(tenant_id=TENANT, email=EMAIL)
    assert drafts_bl.claim(who, draft).outcome is None
    assert read(db, Draft, draft)["outcome"] is None
    drafts_bl.revert(key(draft, conversation))
    drafts_bl.discard(who, draft)
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("discarded", None)


def test_a_checkpoint_sets_it(db, conversation):
    draft = add_draft(
        db,
        conversation,
        state="applying",
        outcome={"confirm_state": STATE},
        updated_at=ago(seconds=5),
    )
    before = read(db, Draft, draft)
    assert drafts_bl.checkpoint(key(draft, conversation), LATER)
    row = read(db, Draft, draft)
    assert row["outcome"] == {"confirm_state": LATER}
    assert row["updated_at"] == before["updated_at"]


def test_a_checkpoint_with_an_outcome_stores_it_in_place_of_the_stored_one(
    db, conversation
):
    draft = add_draft(
        db,
        conversation,
        state="applying",
        outcome={"confirm_state": STATE, "key_of_a_newer_version": 1},
    )
    made = outcome("partially_applied", "ok", OBJECTS[0])
    assert drafts_bl.checkpoint(key(draft, conversation), LATER, made)
    row = read(db, Draft, draft)
    assert row["outcome"] == {**json.loads(made.json()), "confirm_state": LATER}


@pytest.mark.parametrize(
    "values",
    [{"state": "pending"}, {"state": "applied"}, {"created_by": "other@example.com"}],
)
def test_a_checkpoint_of_a_draft_not_being_applied(db, conversation, values):
    draft = add_draft(
        db, conversation, **{"state": "applying", "outcome": None, **values}
    )
    before = read(db, Draft, draft)
    assert not drafts_bl.checkpoint(key(draft, conversation), STATE)
    assert read(db, Draft, draft) == before


@pytest.mark.parametrize(
    "stored", [{"confirm_state": STATE}, {**SO_FAR, "confirm_state": STATE}]
)
def test_keep_applying_keeps_it(db, conversation, stored):
    draft = add_draft(
        db, conversation, state="applying", outcome=stored, updated_at=ago(seconds=5)
    )
    before = read(db, Draft, draft)
    pending = outcome("outcome_pending", "unknown", OBJECTS[0], finished_at=None)
    view = drafts_bl.keep_applying(key(draft, conversation), pending)
    row = read(db, Draft, draft)
    assert row["outcome"] == {**json.loads(pending.json()), "confirm_state": STATE}
    assert row["updated_at"] == before["updated_at"]
    assert "confirm_state" not in view.json()


@pytest.mark.parametrize("merge", [False, True], ids=["resolve", "merged"])
def test_resolve_keeps_it(db, conversation, merge):
    draft = add_draft(
        db, conversation, state="applying", outcome={"confirm_state": STATE}
    )
    final = outcome("applied", "ok", OBJECTS[1])
    view = drafts_bl.resolve(
        key(draft, conversation),
        from_state="applying",
        state="applied",
        event="an event",
        outcome=final,
        by_user=False,
        merge_outcome=merge,
    )
    row = read(db, Draft, draft)
    assert row["outcome"] == {**json.loads(final.json()), "confirm_state": STATE}
    assert "confirm_state" not in view.json()


@pytest.mark.parametrize(
    ("stored", "after"),
    [
        ({"confirm_state": STATE}, None),
        ({**SO_FAR, "confirm_state": STATE}, SO_FAR),
        (None, None),
    ],
    ids=["only-the-state", "with-an-outcome", "none"],
)
def test_revert_clears_it(db, conversation, stored, after):
    draft = add_draft(db, conversation, state="applying", outcome=stored)
    drafts_bl.revert(key(draft, conversation))
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("pending", after)


@pytest.mark.parametrize("stored", [[1, 2], "text", 3])
def test_a_stored_outcome_that_is_not_an_object(db, conversation, stored):
    """Something else wrote it: the statements still work, and nothing is kept."""
    draft = add_draft(db, conversation, state="applying", outcome=stored)
    pending = outcome("outcome_pending", "unknown", finished_at=None)
    drafts_bl.keep_applying(key(draft, conversation), pending)
    assert read(db, Draft, draft)["outcome"] == as_stored(pending)
    change(db, Draft, draft, outcome=stored)
    drafts_bl.revert(key(draft, conversation))
    assert read(db, Draft, draft)["outcome"] == stored
    change(db, Draft, draft, state="applying")
    assert drafts_bl.checkpoint(key(draft, conversation), STATE)
    assert read(db, Draft, draft)["outcome"] == {"confirm_state": STATE}
