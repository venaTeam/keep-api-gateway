"""POST /assistant/drafts/{id}/confirm: the one thing that changes alerts."""
import asyncio
import json
import logging
import uuid

import httpx
import pytest

from src.services.assistant.bl import confirm_bl, drafts_bl, outcomes
from src.services.assistant.core import metrics
from src.services.assistant.core import work as work_module
from src.services.assistant.gateway import client as client_module
from src.services.assistant.models.api.draft import ChangeAlertStatusPayload
from src.services.assistant.models.db import Conversation, Draft
from tests.assistant import golden_stream
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.fakes import TOKEN, whoami
from tests.assistant.logs import rendered
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    ago,
    change,
    draft_payload,
    messages,
    read,
    skipped_alert,
)
from tests.assistant.running import Keep, equip, post_confirm, until

CANARY = "canary-7c01de"
FIRING = {"fp-1": "firing", "fp-2": "firing"}


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def keep():
    return Keep(FIRING)


@pytest.fixture()
def work(app, keep):
    return equip(app, gateway=keep)


@pytest.fixture()
def conversation(db):
    return add_conversation(db, updated_at=ago(days=3))


@pytest.fixture()
def draft(db, work, conversation):
    """Two firing alerts, to be acknowledged."""
    return add_draft(db, conversation, "fp-1", "fp-2")


def state_of(db, draft) -> str:
    return read(db, Draft, draft)["state"]


def counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def by_alert(body: dict) -> dict:
    return {
        r["fingerprint"]: (r["result"], r["status_after"])
        for r in body["outcome"]["results"]
    }


async def confirm(app, draft) -> dict:
    exchange = await post_confirm(app, draft).finish()
    assert exchange.status == 200, exchange.text
    return exchange.json()


# --- the plain case ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_changes_what_the_card_lists(app, db, keep, conversation, draft):
    before = read(db, Conversation, conversation)["updated_at"]
    body = await confirm(app, draft)
    assert keep.statuses == {"fp-1": "acknowledged", "fp-2": "acknowledged"}
    assert set(body) == set(golden_stream.drafts()["applied"].dict())
    assert (body["id"], body["state"]) == (str(draft), "applied")
    assert body["resolved_at"] is not None
    assert body["outcome"].pop("finished_at") is not None
    assert body["outcome"].pop("results") == [
        {
            "fingerprint": fp,
            "name": f"name of {fp}",
            "result": "applied",
            "status_after": "acknowledged",
        }
        for fp in ("fp-1", "fp-2")
    ]
    assert body["outcome"] == {
        "code": "applied",
        "message": "All alerts were changed.",
        "gateway_result": "ok",
        "applied_count": 2,
        "failed_count": 0,
        "unconfirmed_count": 0,
        "skipped_count": 0,
        "not_found_count": 0,
    }
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]["code"]) == ("applied", "applied")
    assert row["resolved_at"] is not None
    assert read(db, Conversation, conversation)["updated_at"] > before


@pytest.mark.asyncio
async def test_the_write_is_the_one_of_the_ui(app, keep, draft):
    await confirm(app, draft)
    [write] = keep.writes
    assert json.loads(write.content) == {
        "fingerprints": ["fp-1", "fp-2"],
        "enrichments": {
            "status": "acknowledged",
            "dismiss_mode": None,
            "dismissed_until": None,
        },
    }
    assert write.url.query == b"dispose_on_new_alert=false"
    assert write.headers["X-Keep-Source"] == "assistant"
    # Looks and write, all on the user's behalf and traceable to the draft.
    for request in keep.looks + keep.writes:
        assert request.headers["Authorization"] == TOKEN
        assert request.headers["X-Request-ID"] == str(draft)
    assert len(keep.looks) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "note", "dispose", "content", "query"),
    [
        (
            "acknowledged",
            None,
            False,
            b'{"fingerprints":["fp-1"],"enrichments":{"status":"acknowledged",'
            b'"dismiss_mode":null,"dismissed_until":null}}',
            b"dispose_on_new_alert=false",
        ),
        (
            "suppressed",
            "maintenance é",
            True,
            b'{"fingerprints":["fp-1"],"enrichments":{"status":"suppressed",'
            b'"note":"maintenance \xc3\xa9"}}',
            b"dispose_on_new_alert=true",
        ),
    ],
    ids=["acknowledge", "suppress-with-note"],
)
async def test_the_request_is_the_same_byte_for_byte(
    app, db, work, keep, conversation, target, note, dispose, content, query
):
    """What the gateway receives, as it receives it."""
    draft = add_draft(
        db,
        conversation,
        payload=draft_payload(
            "fp-1", target=target, note=note, dispose_on_new_alert=dispose
        ),
    )
    await confirm(app, draft)
    [write] = keep.writes
    assert (write.method, write.url.path) == ("POST", "/alerts/batch_enrich")
    assert write.content == content
    assert write.url.query == query
    assert write.headers["Content-Type"] == "application/json"
    assert write.headers["Content-Length"] == str(len(content))


@pytest.mark.asyncio
async def test_a_suppression_with_a_note(app, db, work, keep, conversation):
    draft = add_draft(
        db,
        conversation,
        payload=draft_payload(
            "fp-1", target="suppressed", note="maintenance", dispose_on_new_alert=True
        ),
    )
    assert (await confirm(app, draft))["state"] == "applied"
    [write] = keep.writes
    assert json.loads(write.content)["enrichments"] == {
        "status": "suppressed",
        "note": "maintenance",
    }
    assert write.url.query == b"dispose_on_new_alert=true"


# --- what is written -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alerts_that_changed_since_are_left_alone(
    app, db, work, keep, conversation
):
    keep.statuses = {"fp-1": "firing", "fp-2": "acknowledged"}
    draft = add_draft(db, conversation, "fp-1", "fp-2", "fp-gone")
    body = await confirm(app, draft)
    assert json.loads(keep.writes[0].content)["fingerprints"] == ["fp-1"]
    assert (body["state"], body["outcome"]["code"]) == ("applied", "applied")
    assert by_alert(body) == {
        "fp-1": ("applied", "acknowledged"),
        "fp-2": ("skipped", "acknowledged"),
        "fp-gone": ("not_found", None),
    }
    counts = {k: v for k, v in body["outcome"].items() if k.endswith("_count")}
    assert counts == {
        "applied_count": 1,
        "failed_count": 0,
        "unconfirmed_count": 0,
        "skipped_count": 1,
        "not_found_count": 1,
    }
    # The second look is at what was written.
    assert json.loads(keep.looks[1].content)["cel"] == 'fingerprint in ["fp-1"]'


@pytest.mark.asyncio
async def test_nothing_but_the_alerts_of_the_card(app, db, work, keep, conversation):
    """Not what was skipped or missing at drafting time, and not what a filter would
    match now."""
    keep.statuses = {"fp-1": "firing", "fp-2": "firing", "fp-3": "firing"}
    draft = add_draft(
        db,
        conversation,
        payload=draft_payload(
            "fp-1",
            selector={"type": "cel", "cel": "severity == 'critical'"},
            skipped=[skipped_alert("fp-2")],
            not_found=["fp-3"],
        ),
    )
    body = await confirm(app, draft)
    assert list(by_alert(body)) == ["fp-1"]
    assert json.loads(keep.writes[0].content)["fingerprints"] == ["fp-1"]
    assert keep.statuses["fp-2"] == keep.statuses["fp-3"] == "firing"
    assert all("severity" not in r.content.decode() for r in keep.looks)


@pytest.mark.asyncio
async def test_nothing_left_to_change(app, db, work, keep, conversation):
    keep.statuses = {"fp-1": "acknowledged"}
    draft = add_draft(db, conversation, "fp-1", "fp-gone")
    body = await confirm(app, draft)
    assert keep.writes == []
    assert (body["state"], body["outcome"]["code"]) == ("applied", "nothing_to_change")
    assert body["outcome"]["gateway_result"] is None
    assert body["outcome"]["message"] == "Every alert already had this status."
    assert by_alert(body) == {
        "fp-1": ("skipped", "acknowledged"),
        "fp-gone": ("not_found", None),
    }


# --- what the gateway answers --------------------------------------------------------------


def answer(status: int, body=None, text: str | None = None) -> httpx.Response:
    return (
        httpx.Response(status, text=text)
        if text is not None
        else httpx.Response(status, json=body)
    )


# (answer, gateway_result) of writes whose effect has to be looked up
LOOKED_UP = [
    pytest.param(answer(200, {"status": "ok"}), "ok", id="ok"),
    pytest.param(answer(200, {"status": "failed"}), "failed_response", id="failed"),
    pytest.param(answer(200, ["?"]), "http_error", id="other-200"),
    pytest.param(
        answer(422, {"detail": CANARY}), "rejected_maybe_written", id="422-text"
    ),
    pytest.param(answer(500, {"message": CANARY}), "http_error", id="500"),
    pytest.param(answer(500, text="Internal Server Error"), "http_error", id="500-text"),
    pytest.param(answer(409, {"detail": CANARY}), "http_error", id="409"),
]
# ... where only a look back makes the result known
NOT_OK = LOOKED_UP[1:]
# (answer, gateway_result, code, message) of writes that were refused
REFUSED = [
    pytest.param(
        answer(400, {"detail": CANARY}),
        ("rejected", "failed", "Keep refused the change."),
        id="400",
    ),
    pytest.param(
        answer(422, {"detail": [{"msg": CANARY}]}),
        ("rejected", "failed", "Keep refused the change."),
        id="422-list",
    ),
    pytest.param(
        answer(403, {"detail": "Read only instance, but non-read scopes requested"}),
        ("read_only", "read_only", "Keep is in read-only mode."),
        id="read-only",
    ),
    pytest.param(
        answer(403, {"detail": CANARY}),
        (
            "not_permitted",
            "not_permitted",
            "You do not have permission to change alert status.",
        ),
        id="403",
    ),
    pytest.param(
        answer(403, text="nope"),
        (
            "not_permitted",
            "not_permitted",
            "You do not have permission to change alert status.",
        ),
        id="403-text",
    ),
    pytest.param(
        answer(401, {"detail": CANARY}),
        (
            "unauthenticated",
            "failed",
            "Your session expired before the change was made.",
        ),
        id="401",
    ),
]
UNKNOWN = [
    pytest.param(answer(502), id="502"),
    pytest.param(answer(503), id="503"),
    pytest.param(answer(504), id="504"),
    pytest.param(httpx.ReadTimeout(CANARY), id="timeout"),
    pytest.param(httpx.RemoteProtocolError(CANARY), id="dropped"),
    pytest.param(httpx.WriteError(CANARY), id="cut-off"),
]
NEVER_SENT = [
    pytest.param(httpx.ConnectError(CANARY), id="connect"),
    pytest.param(httpx.ConnectTimeout(CANARY), id="connect-timeout"),
    pytest.param(httpx.PoolTimeout(CANARY), id="pool"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "result"), LOOKED_UP)
async def test_all_changed(app, keep, draft, response, result):
    keep.answer = response
    body = await confirm(app, draft)
    assert (body["state"], body["outcome"]["code"]) == ("applied", "applied")
    assert body["outcome"]["gateway_result"] == result
    assert len(keep.looks) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "result"), LOOKED_UP)
async def test_some_changed(app, keep, draft, response, result):
    keep.answer = response
    keep.stuck = {"fp-2"}
    body = await confirm(app, draft)
    assert body["state"] == body["outcome"]["code"] == "partially_applied"
    assert body["outcome"]["message"] == "Some alerts were changed."
    assert by_alert(body) == {
        "fp-1": ("applied", "acknowledged"),
        "fp-2": ("failed", "firing"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "result"), LOOKED_UP)
async def test_none_changed(app, keep, draft, response, result):
    keep.answer = response
    keep.stuck = {"fp-1", "fp-2"}
    body = await confirm(app, draft)
    assert body["state"] == body["outcome"]["code"] == "failed"
    assert body["outcome"]["message"] == "No alert was changed."
    assert body["outcome"]["gateway_result"] == result
    assert by_alert(body) == {"fp-1": ("failed", "firing"), "fp-2": ("failed", "firing")}


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "expected"), REFUSED)
async def test_refused(app, db, keep, draft, response, expected):
    keep.answer = response
    keep.stuck = {"fp-1", "fp-2"}
    body = await confirm(app, draft)
    outcome = body["outcome"]
    assert (outcome["gateway_result"], outcome["code"], outcome["message"]) == expected
    assert body["state"] == state_of(db, draft) == "failed"
    assert by_alert(body) == {"fp-1": ("failed", None), "fp-2": ("failed", None)}
    assert outcome["finished_at"] is not None
    # Nothing was written, so there is nothing to look at.
    assert len(keep.looks) == 1
    assert CANARY not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", UNKNOWN)
async def test_nobody_knows(app, db, keep, conversation, draft, failure):
    keep.on("POST", "/alerts/batch_enrich", failure)
    body = await confirm(app, draft)
    assert (body["state"], body["outcome"]["code"]) == ("applying", "outcome_pending")
    assert body["outcome"]["message"] == "Keep is still applying this change."
    assert body["outcome"]["gateway_result"] == "unknown"
    assert body["outcome"]["finished_at"] is body["resolved_at"] is None
    assert by_alert(body) == {
        "fp-1": ("unconfirmed", None),
        "fp-2": ("unconfirmed", None),
    }
    # The gateway may still be at it.
    assert len(keep.looks) == 1
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]["code"]) == ("applying", "outcome_pending")
    assert messages(db, conversation) == []


@pytest.mark.asyncio
async def test_it_can_be_looked_at_thirty_seconds_after_the_click(
    app, db, keep, draft
):
    """Not after the answer to it."""
    claimed = ago(seconds=16)

    async def slow(request):
        change(db, Draft, draft, updated_at=claimed)
        return httpx.Response(502)

    keep.on("POST", "/alerts/batch_enrich", slow)
    assert (await confirm(app, draft))["state"] == "applying"
    assert read(db, Draft, draft)["updated_at"] == claimed


@pytest.mark.asyncio
async def test_a_write_that_takes_too_long(app, db, keep, draft, set_env):
    set_env("ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS", 0.2)
    released = asyncio.Event()
    write = keep._write

    async def slow(request):
        await released.wait()
        return await write(request)

    keep.on("POST", "/alerts/batch_enrich", slow)
    body = await confirm(app, draft)
    assert (body["state"], body["outcome"]["code"]) == ("applying", "outcome_pending")
    # It is not cut off.
    released.set()
    await until(lambda: keep.statuses["fp-1"] == "acknowledged")
    assert state_of(db, draft) == "applying"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", NEVER_SENT)
async def test_never_sent(app, db, keep, conversation, draft, failure):
    keep.on("POST", "/alerts/batch_enrich", failure)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("pending", None)
    assert messages(db, conversation) == []
    # Then it works.
    keep.on("POST", "/alerts/batch_enrich", keep._write)
    assert (await confirm(app, draft))["state"] == "applied"


@pytest.mark.asyncio
async def test_too_many_calls_at_once(app, db, keep, draft, monkeypatch):
    real = confirm_bl._Attempt._look_before

    async def then_full(self, payload):
        found = await real(self, payload)
        monkeypatch.setattr(client_module, "MAX_RUNNING_CALLS", 0)
        return found

    monkeypatch.setattr(confirm_bl._Attempt, "_look_before", then_full)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
    assert state_of(db, draft) == "pending"
    assert keep.writes == []


# --- the second look ------------------------------------------------------------------------


def answer_second_look(keep, response) -> None:
    async def query(request):
        if keep.writes:
            if isinstance(response, Exception):
                raise response
            return httpx.Response(response.status_code, content=response.content)
        return await keep._query(request)

    keep.on("POST", "/alerts/query", query)


BLIND = [
    pytest.param(answer(502), id="502"),
    pytest.param(answer(401), id="401"),
    pytest.param(answer(200, {"results": "?"}), id="unusable"),
    pytest.param(httpx.ReadTimeout(CANARY), id="timeout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("second", BLIND)
async def test_accepted_but_not_checked(app, keep, draft, second):
    answer_second_look(keep, second)
    body = await confirm(app, draft)
    assert (body["state"], body["outcome"]["code"]) == ("applied", "unverified")
    assert body["outcome"]["gateway_result"] == "ok"
    assert body["outcome"]["unconfirmed_count"] == 2
    assert body["outcome"]["finished_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("second", BLIND)
@pytest.mark.parametrize(("response", "result"), NOT_OK)
async def test_not_accepted_and_not_checked(app, db, keep, draft, response, result, second):
    keep.answer = response
    answer_second_look(keep, second)
    body = await confirm(app, draft)
    assert (body["state"], body["outcome"]["code"]) == ("applying", "outcome_pending")
    assert body["outcome"]["gateway_result"] == result
    assert state_of(db, draft) == "applying"


@pytest.mark.asyncio
async def test_an_alert_that_is_gone_after_the_write(app, keep, draft):
    answer_second_look(
        keep,
        answer(200, {"results": [{"fingerprint": "fp-1", "status": "acknowledged"}]}),
    )
    body = await confirm(app, draft)
    assert body["state"] == "partially_applied"
    assert by_alert(body) == {
        "fp-1": ("applied", "acknowledged"),
        "fp-2": ("unconfirmed", None),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "state", "code"),
    [
        (answer(200, {"status": "ok"}), "applied", "unverified"),
        (answer(500), "failed", "outcome_unknown"),
    ],
)
async def test_all_alerts_are_gone_after_the_write(
    app, keep, draft, response, state, code
):
    keep.answer = response
    answer_second_look(keep, answer(200, {"results": []}))
    body = await confirm(app, draft)
    assert (body["state"], body["outcome"]["code"]) == (state, code)
    assert body["outcome"]["unconfirmed_count"] == 2


def test_the_messages_are_the_ones_of_the_fixture():
    """Which keep-ui is built against."""
    texts = {
        (d.outcome.code, d.outcome.gateway_result): d.outcome.message
        for d in golden_stream.drafts().values()
        if d.outcome is not None
    }
    assert len({code for code, _ in texts}) == 10
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload())
    for (code, result), text in texts.items():
        assert outcomes._outcome(payload, {}, {}, code, result).message == text


# --- before the write ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "status", "code"),
    [
        (answer(401), 401, "unauthenticated"),
        (answer(403, {"detail": CANARY}), 403, "forbidden"),
        (answer(502), 502, "gateway_unavailable"),
        (answer(200, {"results": "?"}), 502, "gateway_unavailable"),
        (httpx.ReadTimeout(CANARY), 502, "gateway_unavailable"),
    ],
    ids=["401", "403", "502", "unusable", "timeout"],
)
async def test_the_first_look_fails(app, db, keep, draft, first, status, code):
    keep.on("POST", "/alerts/query", first)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (status, code)
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"], row["resolved_at"]) == ("pending", None, None)
    assert keep.writes == []


@pytest.mark.asyncio
async def test_the_first_look_takes_too_long(app, db, keep, draft, monkeypatch):
    monkeypatch.setattr(confirm_bl, "READ_SECONDS", 0.1)

    async def slow(request):
        await asyncio.sleep(1)
        return await keep._query(request)

    keep.on("POST", "/alerts/query", slow)
    exchange = await post_confirm(app, draft).finish(timeout=5)
    assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
    assert state_of(db, draft) == "pending"
    assert keep.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("spare", [0.15, 3], ids=["too-late", "in-time"])
async def test_the_write_starts_only_with_time_left_to_tell(
    app, db, keep, draft, monkeypatch, spare
):
    """Counted from the moment the request came in, identity included."""
    monkeypatch.setattr(
        confirm_bl, "BUDGET_SECONDS", confirm_bl.NEEDED_FOR_WRITE_SECONDS + spare
    )

    async def slow_whoami(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=whoami())

    keep.on("GET", "/whoami", slow_whoami)
    exchange = await post_confirm(app, draft).finish()
    if spare < 0.2:
        assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
        assert state_of(db, draft) == "pending"
        assert keep.writes == []
    else:
        assert exchange.json()["state"] == "applied"


def test_the_budget_holds_before_the_proxy_gives_up():
    assert confirm_bl.BUDGET_SECONDS == 26
    assert confirm_bl.READ_SECONDS == 4
    assert confirm_bl.NEEDED_FOR_WRITE_SECONDS == 17


# --- who may, and when -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("applying", "draft_applying"),
        ("applied", "draft_not_pending"),
        ("partially_applied", "draft_not_pending"),
        ("failed", "draft_not_pending"),
        ("discarded", "draft_not_pending"),
        ("expired", "draft_expired"),
    ],
)
async def test_only_a_pending_draft(app, db, keep, work, conversation, state, code):
    draft = add_draft(db, conversation, state=state)
    before = read(db, Draft, draft)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (409, code)
    assert exchange.json()["draft"]["id"] == str(draft)
    assert exchange.json()["draft"]["state"] == state
    assert read(db, Draft, draft) == before
    assert keep.looks == keep.writes == []


@pytest.mark.asyncio
async def test_an_expired_draft(app, db, keep, work, conversation):
    draft = add_draft(db, conversation, expires_at=ago(seconds=1))
    expired = counter(
        metrics.drafts_total, kind="change_alert_status", event="expired"
    )
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (409, "draft_expired")
    assert exchange.json()["draft"]["state"] == "expired"
    assert exchange.json()["draft"]["resolved_at"] is not None
    assert state_of(db, draft) == "expired"
    assert keep.looks == keep.writes == []
    assert (
        counter(metrics.drafts_total, kind="change_alert_status", event="expired")
        == expired + 1
    )


@pytest.mark.asyncio
async def test_confirmed_twice_at_once(app, db, keep, draft):
    released = asyncio.Event()

    async def slow(request):
        await released.wait()
        return await Keep._query(keep, request)

    keep.on("POST", "/alerts/query", slow)
    first, second = post_confirm(app, draft), post_confirm(app, draft)
    await until(lambda: first.status or second.status)
    released.set()
    answers = sorted(
        [await first.finish(), await second.finish()], key=lambda e: e.status
    )
    assert [e.status for e in answers] == [200, 409]
    assert answers[1].code == "draft_applying"
    assert answers[0].json()["state"] == "applied"
    assert len(keep.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "me", [{"email": "else@example.com"}, {"tenant_id": OTHER_TENANT}], ids=str
)
async def test_someone_elses_draft_does_not_exist(app, db, keep, draft, me):
    keep.on_json("GET", "/whoami", whoami(**me))
    theirs = await post_confirm(app, draft).finish()
    missing = await post_confirm(app, uuid.uuid4()).finish()
    malformed = await post_confirm(app, "not-a-uuid").finish()
    assert (theirs.status, theirs.code) == (404, "draft_not_found")
    assert theirs.json() == missing.json() == malformed.json()
    assert state_of(db, draft) == "pending"
    assert keep.looks == keep.writes == []


@pytest.mark.asyncio
async def test_the_hint_on_the_card_does_not_decide(app, db, work, keep, conversation):
    """The gateway does."""
    draft = add_draft(db, conversation, permitted=False)
    keep.on_json("GET", "/whoami", whoami(scopes=["read:alert"]))
    assert (await confirm(app, draft))["state"] == "applied"


@pytest.mark.asyncio
async def test_without_a_token(app, db, keep, draft):
    exchange = await post_confirm(app, draft, token=None).finish()
    assert (exchange.status, exchange.code) == (401, "unauthenticated")
    assert state_of(db, draft) == "pending"


@pytest.mark.asyncio
async def test_confirming_needs_no_llm(app, draft):
    assert app.state.assistant_llm is None
    assert (await confirm(app, draft))["state"] == "applied"


# --- what is left behind -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_model_learns_the_outcome_without_the_alerts(
    app, db, work, keep, conversation
):
    draft = add_draft(
        db, conversation, payload=draft_payload("fp-1", "fp-2", note="a secret note")
    )
    keep.stuck = {"fp-2"}
    await confirm(app, draft)
    [event] = messages(db, conversation)
    assert (event["role"], event["draft_id"]) == ("event", draft)
    assert event["content"] == (
        f"draft {draft} (change_alert_status to acknowledged): partially_applied. "
        "1 applied, 1 failed, 0 unconfirmed, 0 skipped, 0 not found."
    )


@pytest.mark.asyncio
async def test_all_or_nothing_of_the_result_is_stored(
    app, db, keep, conversation, draft, monkeypatch
):
    """The event row cannot be stored: then the draft does not have its result."""
    monkeypatch.setattr(outcomes, "event_text", lambda *args: "\x00")
    before = read(db, Conversation, conversation)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("applying", None)
    assert read(db, Conversation, conversation) == before
    assert messages(db, conversation) == []


@pytest.mark.asyncio
async def test_a_draft_that_was_settled_meanwhile(app, db, keep, conversation, draft):
    """By a look at the conversation. Its result stands."""
    write = keep._write

    async def settled(request):
        change(db, Draft, draft, state="failed")
        return await write(request)

    keep.on("POST", "/alerts/batch_enrich", settled)
    assert (await confirm(app, draft))["state"] == "failed"
    assert read(db, Draft, draft)["outcome"] is None
    assert messages(db, conversation) == []


@pytest.mark.asyncio
async def test_a_draft_that_was_settled_meanwhile_is_not_put_back(app, db, keep, draft):
    async def settled(request):
        change(db, Draft, draft, state="failed")
        return httpx.Response(502)

    keep.on("POST", "/alerts/query", settled)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
    assert state_of(db, draft) == "failed"


@pytest.mark.asyncio
async def test_it_is_counted(app, keep, draft):
    labels = {"kind": "change_alert_status"}
    confirmed = counter(metrics.drafts_total, event="confirmed", **labels)
    applied = counter(metrics.draft_outcomes_total, outcome="applied", **labels)
    await confirm(app, draft)
    await post_confirm(app, draft).finish()
    assert counter(metrics.drafts_total, event="confirmed", **labels) == confirmed + 1
    assert (
        counter(metrics.draft_outcomes_total, outcome="applied", **labels)
        == applied + 1
    )


# --- a request that goes away, a worker that does ---------------------------------------------


def held_back(keep, path):
    """Requests to `path` wait for the second event, and set the first."""
    reached, released = asyncio.Event(), asyncio.Event()
    real = {"/alerts/query": keep._query, "/alerts/batch_enrich": keep._write}[path]

    async def hold(request):
        reached.set()
        await released.wait()
        return await real(request)

    keep.on("POST", path, hold)
    return reached, released


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/alerts/query", "/alerts/batch_enrich"])
async def test_a_user_who_leaves_does_not_stop_it(app, db, work, keep, draft, path):
    reached, released = held_back(keep, path)
    exchange = post_confirm(app, draft)
    await reached.wait()
    exchange.task.cancel()
    await asyncio.wait({exchange.task}, timeout=5)
    released.set()
    assert await work.settle(5) == 0
    assert state_of(db, draft) == "applied"
    assert len(keep.writes) == 1


@pytest.mark.asyncio
async def test_a_drain_before_the_write(app, db, work, keep, conversation, draft):
    reached, released = held_back(keep, "/alerts/query")
    exchange = post_confirm(app, draft)
    await reached.wait()
    work.drain()
    released.set()
    await exchange.finish(timeout=5)
    assert (exchange.status, exchange.code) == (503, "not_ready")
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("pending", None)
    assert keep.writes == []


@pytest.mark.asyncio
async def test_a_drain_at_the_moment_of_the_write(app, db, work, keep, draft):
    """After the last check that could be made ahead of the send."""
    real = work.wait

    async def drained_meanwhile(task, **options):
        work.drain()
        await real(task, **options)

    work.wait = drained_meanwhile
    exchange = await post_confirm(app, draft).finish(timeout=5)
    assert (exchange.status, exchange.code) == (503, "not_ready")
    assert state_of(db, draft) == "pending"
    assert keep.writes == []


@pytest.mark.asyncio
async def test_a_drain_after_the_write_was_sent(
    app, db, work, keep, draft, monkeypatch
):
    monkeypatch.setattr(work_module, "SHUTDOWN_GRACE_SECONDS", 2.0)
    reached, released = held_back(keep, "/alerts/batch_enrich")
    exchange = post_confirm(app, draft)
    await reached.wait()
    loop = asyncio.get_running_loop()
    work.drain()
    started = loop.time()
    await exchange.finish(timeout=5)
    assert 0.3 < loop.time() - started < 2
    assert exchange.json()["state"] == state_of(db, draft) == "applying"
    assert exchange.json()["outcome"]["code"] == "outcome_pending"
    # The write is still given its time.
    released.set()
    assert await app.state.assistant_gateway.wait_for_writes(2) == 0
    assert keep.statuses["fp-1"] == "acknowledged"


@pytest.mark.asyncio
async def test_a_write_that_ends_during_the_drain(app, db, work, keep, draft):
    reached, released = held_back(keep, "/alerts/batch_enrich")
    exchange = post_confirm(app, draft)
    await reached.wait()
    work.drain()
    await asyncio.sleep(0.2)
    released.set()
    await exchange.finish(timeout=5)
    assert exchange.json()["state"] == state_of(db, draft) == "applied"


@pytest.mark.asyncio
async def test_after_the_drain(app, db, work, keep, draft):
    work.drain()
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (503, "not_ready")
    assert state_of(db, draft) == "pending"
    assert keep.looks == []


# --- bugs -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bug_before_the_write_puts_the_draft_back(
    app, db, keep, draft, monkeypatch, caplog
):
    def fail(*args):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(outcomes, "before_write", fail)
    with caplog.at_level(logging.DEBUG):
        exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert state_of(db, draft) == "pending"
    assert CANARY not in exchange.text
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert [
        r.error_type for r in caplog.records if r.message == "assistant: confirm failed"
    ] == ["RuntimeError"]


@pytest.mark.asyncio
async def test_a_bug_after_the_write_does_not(
    app, db, keep, draft, monkeypatch, caplog
):
    def fail(*args):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(outcomes, "verified", fail)
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert state_of(db, draft) == "applying"
    assert len(keep.writes) == 1
    again = await post_confirm(app, draft).finish()
    assert (again.status, again.code) == (409, "draft_applying")


@pytest.mark.asyncio
async def test_a_draft_that_cannot_be_put_back(
    app, db, keep, draft, monkeypatch, caplog
):
    def fail(*args):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(drafts_bl, "revert", fail)
    keep.on("POST", "/alerts/query", answer(502))
    with caplog.at_level(logging.DEBUG):
        exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (502, "gateway_unavailable")
    assert state_of(db, draft) == "applying"
    assert not [r for r in caplog.records if CANARY in rendered(r)]


@pytest.mark.asyncio
async def test_a_kind_this_version_does_not_know(app, db, keep, work, conversation):
    draft = add_draft(db, conversation, kind="from_the_future")
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (500, "internal")
    assert state_of(db, draft) == "pending"
    assert keep.writes == []
