"""POST /assistant/drafts/{id}/discard."""
import uuid

import pytest

from src.services.assistant.bl import drafts_bl
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import VALIDATION_MESSAGES
from src.services.assistant.kinds import alert_status
from src.services.assistant.models.db import Conversation, Draft
from tests.assistant import golden_stream
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.fakes import whoami
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    ago,
    change,
    messages,
    read,
)
from tests.assistant.running import Exchange, Keep, equip, post_confirm


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
def conversation(db, keep):
    return add_conversation(db, updated_at=ago(days=3))


def post(app, draft, **options) -> Exchange:
    return Exchange(app, "POST", f"/assistant/drafts/{draft}/discard", **options)


def discarded() -> float:
    return metrics.drafts_total.labels(
        kind="change_alert_status", event="discarded"
    )._value.get()


@pytest.mark.asyncio
async def test_it_is_discarded(app, db, keep, conversation):
    draft = add_draft(db, conversation)
    before = read(db, Conversation, conversation)["updated_at"]
    count = discarded()
    exchange = await post(app, draft).finish()
    assert exchange.status == 200
    body = exchange.json()
    assert (body["id"], body["state"]) == (str(draft), "discarded")
    assert body["resolved_at"] is not None
    assert body["outcome"] is None
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]) == ("discarded", None)
    assert read(db, Conversation, conversation)["updated_at"] > before
    assert discarded() == count + 1
    # Nothing was asked of the gateway but who this is.
    assert keep.paths() == ["GET /whoami"]


@pytest.mark.asyncio
async def test_the_model_is_told(app, db, conversation):
    draft = add_draft(db, conversation)
    await post(app, draft).finish()
    [event] = messages(db, conversation)
    assert (event["role"], event["draft_id"]) == ("event", draft)
    assert event["content"] == (
        f"draft {draft} (change_alert_status to acknowledged): discarded by the user."
    )


@pytest.mark.asyncio
async def test_a_kind_this_version_does_not_know(app, db, keep, conversation):
    """Put away all the same, and the model is told in words that claim nothing about
    a payload this version cannot read."""
    kind = "from_the_future"
    draft = add_draft(
        db,
        conversation,
        kind=kind,
        payload={"target_status": "acknowledged", "name": "Night shift"},
    )
    count = metrics.drafts_total.labels(kind=kind, event="discarded")._value.get()
    exchange = await post(app, draft).finish()
    assert exchange.status == 200, exchange.text
    assert (exchange.json()["kind"], exchange.json()["state"]) == (kind, "discarded")
    assert read(db, Draft, draft)["state"] == "discarded"
    [event] = messages(db, conversation)
    assert event["content"] == f"draft {draft} ({kind}): discarded by the user."
    assert (
        metrics.drafts_total.labels(kind=kind, event="discarded")._value.get()
        == count + 1
    )
    assert keep.writes == []


@pytest.mark.asyncio
async def test_again(app, db, conversation):
    draft = add_draft(db, conversation)
    first = await post(app, draft).finish()
    before = read(db, Conversation, conversation)
    count = discarded()
    second = await post(app, draft).finish()
    assert (second.status, second.json()) == (200, first.json())
    assert read(db, Conversation, conversation) == before
    assert len(messages(db, conversation)) == 1
    assert discarded() == count


@pytest.mark.asyncio
async def test_a_draft_that_has_run_out_can_still_be_put_away(app, db, conversation):
    draft = add_draft(db, conversation, expires_at=ago(minutes=1))
    assert (await post(app, draft).finish()).json()["state"] == "discarded"


@pytest.mark.asyncio
async def test_then_it_cannot_be_confirmed(app, db, keep, conversation):
    draft = add_draft(db, conversation)
    await post(app, draft).finish()
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (409, "draft_not_pending")
    assert keep.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("applying", "draft_applying"),
        ("applied", "draft_not_pending"),
        ("partially_applied", "draft_not_pending"),
        ("failed", "draft_not_pending"),
        ("expired", "draft_expired"),
    ],
)
async def test_only_a_pending_draft(app, db, conversation, state, code):
    draft = add_draft(db, conversation, state=state)
    before = read(db, Draft, draft)
    exchange = await post(app, draft).finish()
    assert (exchange.status, exchange.code) == (409, code)
    assert exchange.json()["draft"]["state"] == state
    assert read(db, Draft, draft) == before
    assert messages(db, conversation) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "status", "code"),
    [
        ("applying", 409, "draft_applying"),
        ("expired", 409, "draft_expired"),
        ("applied", 409, "draft_not_pending"),
        # Put back since: whoever confirmed it may do so again.
        ("pending", 409, "draft_applying"),
        ("discarded", 200, None),
    ],
)
async def test_somebody_was_faster(
    app, db, conversation, monkeypatch, state, status, code
):
    """Between reading the draft and changing it."""
    draft = add_draft(db, conversation)
    count = discarded()

    def too_late(*args, **kwargs):
        change(db, Draft, draft, state=state)
        return None

    monkeypatch.setattr(drafts_bl, "resolve", too_late)
    exchange = await post(app, draft).finish()
    assert exchange.status == status
    if code:
        assert exchange.code == code
    shown = exchange.json()["draft"] if code else exchange.json()
    assert shown["state"] == state
    assert discarded() == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "me", [{"email": "else@example.com"}, {"tenant_id": OTHER_TENANT}], ids=str
)
async def test_someone_elses_draft_does_not_exist(app, db, keep, conversation, me):
    draft = add_draft(db, conversation)
    keep.on_json("GET", "/whoami", whoami(**me))
    theirs = await post(app, draft).finish()
    missing = await post(app, uuid.uuid4()).finish()
    malformed = await post(app, "not-a-uuid").finish()
    assert (theirs.status, theirs.code) == (404, "draft_not_found")
    assert theirs.json() == missing.json() == malformed.json()
    assert read(db, Draft, draft)["state"] == "pending"


@pytest.mark.asyncio
async def test_without_a_token(app, db, conversation):
    draft = add_draft(db, conversation)
    exchange = await post(app, draft, token=None).finish()
    assert (exchange.status, exchange.code) == (401, "unauthenticated")
    assert read(db, Draft, draft)["state"] == "pending"


# --- put away to finish in the form ---------------------------------------------------

OPENED = {"reason": "opened_in_form"}


@pytest.fixture()
def has_a_form(monkeypatch):
    """A kind with a form to finish in, as presets and correlation rules have. What
    the model is told is recorded."""
    monkeypatch.setattr(alert_status.AlertStatusKind, "can_open_in_form", True)
    told = []
    text = alert_status.AlertStatusKind.discarded_text

    def recorded(self, draft, reason):
        told.append(reason)
        return text(self, draft, reason)

    monkeypatch.setattr(alert_status.AlertStatusKind, "discarded_text", recorded)
    return told


def opened_in_form() -> float:
    return metrics.drafts_total.labels(
        kind="change_alert_status", event="opened_in_form"
    )._value.get()


def refused(exchange, field: str) -> None:
    message = VALIDATION_MESSAGES["invalid_value"]
    assert exchange.status == 400, exchange.text
    assert exchange.json() == {
        "errors": [{"field": field, "code": "invalid_value", "message": message}],
        "detail": message,
    }


@pytest.mark.asyncio
async def test_opened_in_form(app, db, keep, conversation, has_a_form):
    """Put away as any discard, with an outcome that says why, and counted apart."""
    draft = add_draft(db, conversation)
    counts = discarded(), opened_in_form()
    exchange = await post(app, draft, body=OPENED).finish()
    assert exchange.status == 200, exchange.text
    body = exchange.json()
    assert (body["id"], body["state"]) == (str(draft), "discarded")
    assert body["outcome"].pop("finished_at") is not None
    golden = golden_stream.outcomes()["opened_in_form"].dict(exclude={"finished_at"})
    assert body["outcome"] == golden
    row = read(db, Draft, draft)
    assert (row["state"], row["outcome"]["code"]) == ("discarded", "opened_in_form")
    assert (discarded(), opened_in_form()) == (counts[0], counts[1] + 1)
    assert has_a_form == ["opened_in_form"]
    [event] = messages(db, conversation)
    assert event["draft_id"] == draft
    assert keep.paths() == ["GET /whoami"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [None, {}, {"reason": None}, {"reason": "from_the_future"}, {"reason": 5}],
    ids=["none", "empty", "null", "newer", "number"],
)
async def test_no_reason_this_version_knows(app, db, conversation, has_a_form, body):
    """A plain discard: a newer keep-ui may say why in words this one does not know."""
    draft = add_draft(db, conversation)
    counts = discarded(), opened_in_form()
    exchange = await post(app, draft, body=body).finish()
    assert exchange.status == 200, exchange.text
    assert (exchange.json()["state"], exchange.json()["outcome"]) == ("discarded", None)
    assert (discarded(), opened_in_form()) == (counts[0] + 1, counts[1])
    assert has_a_form == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["change_alert_status", "from_the_future"], ids=["no-form", "unknown"]
)
async def test_opened_in_form_of_a_kind_without_a_form(app, db, conversation, kind):
    draft = add_draft(db, conversation, kind=kind)
    before = read(db, Draft, draft)
    refused(await post(app, draft, body=OPENED).finish(), "reason")
    assert read(db, Draft, draft) == before
    assert messages(db, conversation) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "opened_in_form", 5], ids=str)
async def test_a_body_that_is_not_an_object(app, db, conversation, body):
    draft = add_draft(db, conversation)
    before = read(db, Draft, draft)
    refused(await post(app, draft, body=body).finish(), "body")
    assert read(db, Draft, draft) == before


@pytest.mark.asyncio
async def test_opened_in_form_again(app, db, conversation, has_a_form):
    """Whichever way it was put away first."""
    draft = add_draft(db, conversation)
    first = await post(app, draft).finish()
    count = opened_in_form()
    second = await post(app, draft, body=OPENED).finish()
    assert (second.status, second.json()) == (200, first.json())
    assert second.json()["outcome"] is None
    assert len(messages(db, conversation)) == 1
    assert opened_in_form() == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("applying", "draft_applying"),
        ("applied", "draft_not_pending"),
        ("expired", "draft_expired"),
    ],
)
async def test_opened_in_form_only_while_pending(
    app, db, conversation, has_a_form, state, code
):
    """Applying: it may still change, and the form would save it a second time."""
    draft = add_draft(db, conversation, state=state)
    before = read(db, Draft, draft)
    exchange = await post(app, draft, body=OPENED).finish()
    assert (exchange.status, exchange.code) == (409, code)
    assert exchange.json()["draft"]["state"] == state
    assert read(db, Draft, draft) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "me", [{"email": "else@example.com"}, {"tenant_id": OTHER_TENANT}], ids=str
)
@pytest.mark.parametrize("body", [OPENED, [], {"reason": 5}], ids=str)
async def test_someone_elses_draft_does_not_exist_for_any_body(
    app, db, keep, conversation, has_a_form, me, body
):
    draft = add_draft(db, conversation)
    keep.on_json("GET", "/whoami", whoami(**me))
    theirs = await post(app, draft, body=body).finish()
    missing = await post(app, uuid.uuid4(), body=body).finish()
    assert (theirs.status, theirs.code) == (404, "draft_not_found")
    assert theirs.json() == missing.json()
    assert read(db, Draft, draft)["state"] == "pending"
