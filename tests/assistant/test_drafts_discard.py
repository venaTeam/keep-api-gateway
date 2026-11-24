"""POST /assistant/drafts/{id}/discard."""
import uuid

import pytest

from src.services.assistant.bl import drafts_bl
from src.services.assistant.core import metrics
from src.services.assistant.models.db import Conversation, Draft
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
