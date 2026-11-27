"""Plans of several writes, and the time rule: a write is sent only with the time left
to send every write after it and tell the result. Every timing here is a fake clock's."""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Optional

import httpx
import pytest
from pydantic import BaseModel

from src.services.assistant.bl import confirm_bl
from src.services.assistant.config import MAX_WRITE_TIMEOUT_SECONDS
from src.services.assistant.core import metrics
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.client import write_timeout
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.kinds import registry
from src.services.assistant.kinds.alert_status import status_change
from src.services.assistant.kinds.base import (
    ConfirmContext,
    ConfirmPlan,
    Decision,
    DraftKind,
    Sent,
    Step,
    WriteRequest,
)
from src.services.assistant.kinds.registry import KINDS
from src.services.assistant.models.api.draft import (
    ChangeAlertStatusPayload,
    DraftObject,
    DraftOutcome,
    DraftView,
    StoredOutcome,
)
from src.services.assistant.models.db import Draft
from tests.assistant import golden_stream
from tests.assistant.fakes import whoami
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    add_message,
    ago,
    change,
    read,
)
from tests.assistant.running import (
    Exchange,
    Keep,
    equip,
    post_confirm,
    use_fake_clock,
)

FINGERPRINTS = ("fp-1", "fp-2")
# What each write of the fake kind made.
OBJECTS = (
    DraftObject(type="preset", id=golden_stream.uid(901)),
    DraftObject(type="preset", id=golden_stream.uid(902)),
)
NAME = "two_writes"


def outcome(
    code: str, result: Optional[str], made: Optional[DraftObject] = None, **values
) -> DraftOutcome:
    return DraftOutcome(
        **{
            "code": code,
            "message": f"text of {code}",
            "gateway_result": result,
            "applied_count": 0,
            "failed_count": 0,
            "unconfirmed_count": 0,
            "skipped_count": 0,
            "not_found_count": 0,
            "results": [],
            "finished_at": datetime.now(timezone.utc),
            "object": made,
            **values,
        }
    )


class _Plan:
    """Write k acknowledges fingerprint k; each write makes OBJECTS[k]."""

    def __init__(self, kind: "Writes", ctx: ConfirmContext):
        self.kind = kind
        self.ctx = ctx

    def plan(self) -> ConfirmPlan:
        return ConfirmPlan(
            rows=self.kind.rows,
            first=self.first,
            after=self.after,
            pending=self.pending,
            stopped=self.stopped,
        )

    def request(self, k: int) -> WriteRequest:
        body, query = status_change([FINGERPRINTS[k]], "acknowledged", None, False)
        return WriteRequest(
            self.kind.endpoints[k],
            body,
            query=query,
            read_after_seconds=self.kind.read_after[k],
        )

    async def first(self) -> Step:
        self.kind.looks.append(self.ctx.read_seconds())
        return self.request(0)

    async def after(self, sent: Sequence[Sent]) -> Step:
        self.kind.looks.append(self.ctx.read_seconds())
        result = sent[-1].answer.result
        earlier = OBJECTS[len(sent) - 2] if len(sent) > 1 else None
        if result != "ok":
            return "failed", outcome("failed", result, earlier)
        if len(sent) < len(self.kind.endpoints):
            return self.request(len(sent))
        return "applied", outcome("applied", "ok", OBJECTS[len(sent) - 1])

    def pending(self, sent: Sequence[Sent]) -> DraftOutcome:
        earlier = OBJECTS[len(sent) - 2] if len(sent) > 1 else None
        return outcome(
            "outcome_pending", sent[-1].answer.result, earlier, finished_at=None
        )

    def stopped(self, sent: Sequence[Sent]) -> Decision:
        return "partially_applied", outcome(
            "partially_applied", sent[-1].answer.result, OBJECTS[len(sent) - 1]
        )


class Writes(DraftKind):
    """A kind of batch_enrich writes, one per fingerprint."""

    name = NAME
    payload_model = ChangeAlertStatusPayload
    scope_hint = frozenset({"write:alert"})

    def __init__(
        self, rows=("batch_enrich", "batch_enrich"), endpoints=None, read_after=None
    ):
        self.rows = tuple(rows)
        self.endpoints = tuple(endpoints or rows)
        self.read_after = tuple(read_after or [4.0] * len(self.endpoints))
        # What read_seconds() said at each look.
        self.looks: list[float] = []
        self.reconciled: list[tuple[DraftView, Any]] = []

    def write_scope(self, payload: BaseModel) -> str:
        return "write:alert"

    def plan(self, ctx: ConfirmContext) -> ConfirmPlan:
        return _Plan(self, ctx).plan()

    async def reconcile(
        self,
        reader: ReadGateway,
        view: DraftView,
        confirm_state: Optional[Mapping[str, Any]],
    ) -> Optional[Decision]:
        self.reconciled.append((view, confirm_state))
        found = await reader.fetch_alerts_by_fingerprints(["fp-2"])
        if found.found["fp-2"].data["status"] != "acknowledged":
            return None
        return "applied", outcome("applied", "ok", OBJECTS[1])

    def close_unknown(
        self,
        payload: BaseModel,
        stored: Optional[StoredOutcome],
        confirm_state: Optional[Mapping[str, Any]],
    ) -> Decision:
        return "failed", outcome("outcome_unknown", "unknown")

    def event_text(self, draft: DraftView, outcome: DraftOutcome) -> str:
        return f"draft {draft.id} ({draft.kind}): {outcome.code}."

    def discarded_text(self, draft: DraftView, reason: Optional[str]) -> str:
        return f"draft {draft.id} ({draft.kind}): discarded by the user."


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


def register(monkeypatch, kind: Writes) -> Writes:
    monkeypatch.setattr(registry, "KINDS", MappingProxyType({**KINDS, NAME: kind}))
    return kind


@pytest.fixture()
def kind(monkeypatch):
    return register(monkeypatch, Writes())


@pytest.fixture()
def draft(db, work, conversation, kind):
    return add_draft(db, conversation, *FINGERPRINTS, kind=NAME)


def refusals(step: int) -> float:
    return metrics.confirm_budget_refusals_total.labels(
        kind=NAME, step=str(step)
    )._value.get()


def on_write(keep: Keep, *handlers) -> None:
    """The n-th write is answered by the n-th handler, given the request."""
    count = iter(handlers)

    async def write(request: httpx.Request) -> httpx.Response:
        return await next(count)(request)

    keep.on("POST", "/alerts/batch_enrich", write)


async def look(app, conversation) -> dict:
    exchange = await Exchange(
        app, "GET", f"/assistant/conversations/{conversation}"
    ).finish()
    assert exchange.status == 200, exchange.text
    [draft] = [i["draft"] for i in exchange.json()["items"] if i["type"] == "draft"]
    return draft


# --- the rule -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeouts", "k", "read_after", "needed"),
    [
        # One write at the longest the setting allows.
        ([12.0], 0, 4.0, 12.1 + 4.1 + 1),
        ([12.0], 0, 0.0, 12.1 + 0.1 + 1),
        ([5.0], 0, 2.5, 5.1 + 2.6 + 1),
        # v2's incident: two object writes.
        ([8.0, 8.0], 0, 4.0, 8.1 + 8.1 + 4.1 + 2),
        ([8.0, 8.0], 1, 4.0, 8.1 + 4.1 + 1),
        ([8.0, 8.0], 1, 1.0, 8.1 + 1.1 + 1),
        ([5.0, 12.0, 8.0], 1, 4.0, 12.1 + 8.1 + 4.1 + 2),
        ([5.0, 12.0, 8.0], 2, 0.0, 8.1 + 0.1 + 1),
    ],
)
def test_the_time_a_write_needs(timeouts, k, read_after, needed):
    assert confirm_bl.seconds_needed(timeouts, k, read_after) == pytest.approx(needed)


def test_the_first_of_two_writes_needs_22_3_seconds():
    """With two 8 s rows the claim and the look must end within 3.7 s."""
    assert confirm_bl.seconds_needed([8.0, 8.0], 0, 4.0) == pytest.approx(22.3)
    assert confirm_bl.BUDGET_SECONDS - 22.3 == pytest.approx(3.7)


def test_every_kinds_rows_fit_the_budget(set_env):
    """The claim, the look before, the checkpoint before the first write, and the rule
    for it: every plan a golden draft of a kind makes fits in Confirm's time, with
    batch_enrich at its longest."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", MAX_WRITE_TIMEOUT_SECONDS)
    assert write_timeout("batch_enrich") == MAX_WRITE_TIMEOUT_SECONDS
    drafts = json.loads(golden_stream.FIXTURE.read_bytes())["drafts"].values()
    checked = set()
    for stored in drafts:
        kind = KINDS[stored["kind"]]
        plan = kind.plan(
            ConfirmContext(
                reader=None,  # type: ignore[arg-type]  # nothing is read until first()
                caller=None,  # type: ignore[arg-type]
                payload=kind.payload_model.parse_obj(stored["payload"]),
                confirm_input=None,
                read_seconds=lambda: confirm_bl.READ_SECONDS,
            )
        )
        assert plan.rows, kind.name
        timeouts = [write_timeout(row) for row in plan.rows]
        needed = (
            0.5  # the claim
            + confirm_bl.READ_SECONDS  # the look before the first write
            + 1  # the checkpoint before it
            + confirm_bl.seconds_needed(timeouts, 0, confirm_bl.READ_SECONDS)
        )
        assert needed <= confirm_bl.BUDGET_SECONDS, (kind.name, plan.rows)
        checked.add(kind.name)
    assert checked == set(KINDS)


# --- two writes ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_writes_of_12_seconds_do_not_fit(app, db, keep, draft):
    """30.3 s of the 26: the draft goes back before anything is sent."""
    before = refusals(0)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("pending", None)
    assert keep.writes == []
    assert refusals(0) == before + 1


@pytest.mark.asyncio
async def test_two_writes_of_5_seconds_do(app, db, keep, draft, set_env):
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    exchange = await post_confirm(app, draft).finish()
    assert exchange.status == 200, exchange.text
    body = exchange.json()
    assert (body["state"], body["outcome"]["code"]) == ("applied", "applied")
    assert body["outcome"]["object"]["id"] == OBJECTS[1].id
    assert [json.loads(w.content)["fingerprints"] for w in keep.writes] == [
        ["fp-1"],
        ["fp-2"],
    ]
    assert keep.statuses == {"fp-1": "acknowledged", "fp-2": "acknowledged"}


@pytest.mark.asyncio
@pytest.mark.parametrize("spare", [-0.05, 0.05], ids=["too-late", "in-time"])
async def test_a_later_write_without_the_time_left(
    app, db, keep, draft, set_env, clock, spare
):
    """Write 2 of two 5 s rows needs 5.1 + 4.1 + 1 = 10.2 s. Without them, what the
    first write made stands."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    before = refusals(1)

    async def slow_first(request):
        clock.advance(confirm_bl.BUDGET_SECONDS - 10.2 - spare)
        return await keep._write(request)

    on_write(keep, slow_first, keep._write)
    exchange = await post_confirm(app, draft).finish()
    assert exchange.status == 200, exchange.text
    body = exchange.json()
    if spare < 0:
        assert (body["state"], body["outcome"]["code"]) == (
            "partially_applied",
            "partially_applied",
        )
        assert body["outcome"]["object"]["id"] == OBJECTS[0].id
        assert len(keep.writes) == 1
        assert refusals(1) == before + 1
        assert read(db, Draft, draft)["state"] == "partially_applied"
    else:
        assert body["state"] == "applied"
        assert len(keep.writes) == 2
        assert refusals(1) == before


@pytest.mark.asyncio
async def test_a_first_write_refused_sends_no_second(app, db, keep, draft, set_env):
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    keep.answer = httpx.Response(400, json={"detail": "no"})
    body = (await post_confirm(app, draft).finish()).json()
    assert (body["state"], body["outcome"]["code"]) == ("failed", "failed")
    assert body["outcome"]["gateway_result"] == "rejected"
    assert len(keep.writes) == 1


@pytest.mark.asyncio
async def test_a_second_write_nobody_knows_the_end_of(
    app, db, keep, kind, conversation, draft, set_env
):
    """The draft stays applying with the first write's object, and a later look at the
    conversation settles it."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)

    async def written_but_unanswered(request):
        await keep._write(request)
        return httpx.Response(502)

    on_write(keep, keep._write, written_but_unanswered)
    body = (await post_confirm(app, draft).finish()).json()
    assert (body["state"], body["outcome"]["code"]) == ("applying", "outcome_pending")
    assert body["outcome"]["gateway_result"] == "unknown"
    assert body["outcome"]["object"]["id"] == OBJECTS[0].id
    assert read(db, Draft, draft)["state"] == "applying"
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    change(db, Draft, draft, updated_at=ago(seconds=31))
    shown = await look(app, conversation)
    assert (shown["state"], shown["outcome"]["object"]["id"]) == (
        "applied",
        OBJECTS[1].id,
    )
    [(_, confirm_state)] = kind.reconciled
    # Write 2 (from 0: 1) may have been sent, after write 1, which made no object.
    assert (confirm_state["step"], confirm_state["object_ids"]) == (1, [None])


@pytest.mark.asyncio
async def test_a_later_write_the_client_refuses(
    app, db, keep, draft, set_env, monkeypatch
):
    """Nothing of it was sent: what the first one made stands."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)

    async def then_full(request):
        answer = await keep._write(request)
        monkeypatch.setattr(client_module, "MAX_RUNNING_CALLS", 0)
        return answer

    on_write(keep, then_full)
    body = (await post_confirm(app, draft).finish()).json()
    assert (body["state"], body["outcome"]["object"]["id"]) == (
        "partially_applied",
        OBJECTS[0].id,
    )
    assert len(keep.writes) == 1


@pytest.mark.asyncio
async def test_a_drain_between_two_writes(app, db, keep, work, draft, set_env):
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)

    async def drained_meanwhile(request):
        work.drain()
        return await keep._write(request)

    on_write(keep, drained_meanwhile)
    exchange = await post_confirm(app, draft).finish(timeout=5)
    assert exchange.status == 200, exchange.text
    body = exchange.json()
    assert (body["state"], body["outcome"]["object"]["id"]) == (
        "partially_applied",
        OBJECTS[0].id,
    )
    assert len(keep.writes) == 1


# --- plans that are bugs -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        {"endpoints": ("batch_enrich_everything", "batch_enrich")},
        {"rows": ("no_such_row",)},
        {"read_after": (4.5, 4.0)},
        {"read_after": (-1.0, 4.0)},
    ],
    ids=[
        "another-endpoint",
        "a-row-the-client-has-not",
        "a-longer-look",
        "a-negative-look",
    ],
)
async def test_a_first_write_the_plan_did_not_declare(
    app, db, keep, work, conversation, set_env, monkeypatch, plan
):
    """Nothing is sent, and the draft goes back."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    register(monkeypatch, Writes(**plan))
    draft = add_draft(db, conversation, *FINGERPRINTS, kind=NAME)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert read(db, Draft, draft)["state"] == "pending"
    assert keep.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        {"endpoints": ("batch_enrich", "batch_enrich_everything")},
        {"rows": ("batch_enrich",), "endpoints": ("batch_enrich", "batch_enrich")},
    ],
    ids=["another-endpoint", "more-writes-than-rows"],
)
async def test_a_later_write_the_plan_did_not_declare(
    app, db, keep, work, conversation, set_env, monkeypatch, plan
):
    """After a write nothing puts the draft back."""
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    register(monkeypatch, Writes(**plan))
    draft = add_draft(db, conversation, *FINGERPRINTS, kind=NAME)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert read(db, Draft, draft)["state"] == "applying"
    assert len(keep.writes) == 1


# --- the look after the last write ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_after", "sent"), [(0.0, True), (4.0, False)], ids=["no-look", "a-look"]
)
async def test_the_last_write_needs_time_for_its_own_look_only(
    app, db, keep, work, conversation, monkeypatch, clock, read_after, sent
):
    """15 s left: enough for 12.1 + 0.1 + 1 = 13.2, not for 12.1 + 4.1 + 1 = 17.2."""
    register(monkeypatch, Writes(rows=("batch_enrich",), read_after=(read_after,)))
    draft = add_draft(db, conversation, *FINGERPRINTS, kind=NAME)

    async def slow_whoami(request):
        clock.advance(confirm_bl.BUDGET_SECONDS - 15)
        return httpx.Response(200, json=whoami())

    keep.on("GET", "/whoami", slow_whoami)
    exchange = await post_confirm(app, draft).finish()
    if sent:
        assert exchange.json()["state"] == "applied"
        assert len(keep.writes) == 1
    else:
        assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
        assert keep.writes == []


@pytest.mark.asyncio
async def test_the_look_after_a_write_takes_what_the_write_asked_for(
    app, db, keep, work, conversation, monkeypatch, set_env
):
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 5)
    kind = register(monkeypatch, Writes(read_after=(2.5, 1.0)))
    draft = add_draft(db, conversation, *FINGERPRINTS, kind=NAME)
    assert (await post_confirm(app, draft).finish()).json()["state"] == "applied"
    # Before the first write, after it, after the second.
    assert kind.looks == [4.0, 2.5, 1.0]
