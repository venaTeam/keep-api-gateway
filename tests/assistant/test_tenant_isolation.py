"""Rows belong to (tenant, email). For anyone else they do not exist."""
import dataclasses
import uuid

import pytest

from src.services.assistant.bl import drafts_bl, timeline
from src.services.assistant.bl.conversations_bl import (
    NewMessage,
    TurnLost,
    add_messages,
    end_turn,
    start_turn,
)
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.kinds import alert_set
from src.services.assistant.models.api.draft import ChangeAlertStatusPayload
from src.services.assistant.models.db import Conversation, Draft
from tests.assistant.conftest import OTHER_TENANT, TENANT
from tests.assistant.fakes import identity
from tests.assistant.rows import (
    EMAIL,
    add_conversation,
    add_draft,
    add_message,
    ago,
    draft_payload,
    messages,
    read,
)

STRANGERS = {
    "another user": identity(email="else@example.com"),
    "the same email in another tenant": identity(tenant_id=OTHER_TENANT),
}


@pytest.fixture(params=STRANGERS.values(), ids=STRANGERS.keys())
def stranger(request):
    return request.param


@pytest.fixture()
def db(clean_db):
    return clean_db


def test_a_message_into_someone_elses_conversation_is_not_found(db, stranger):
    conversation = add_conversation(db)
    with pytest.raises(AssistantError) as caught:
        start_turn(stranger, conversation, "hello")
    assert (caught.value.code, caught.value.status) == ("conversation_not_found", 404)
    assert messages(db, conversation) == []
    assert read(db, Conversation, conversation)["active_turn_id"] is None


def test_a_running_conversation_of_someone_else_is_not_found_either(db, stranger):
    turn = start_turn(identity(), None, "mine")
    with pytest.raises(AssistantError) as caught:
        start_turn(stranger, turn.conversation_id, "hello")
    assert caught.value.code == "conversation_not_found"


def test_a_turn_cannot_write_as_someone_else(db, stranger):
    turn = start_turn(identity(), None, "mine")
    forged = dataclasses.replace(
        turn, tenant_id=stranger.tenant_id, created_by=stranger.email
    )
    with pytest.raises(TurnLost):
        add_messages(forged, [NewMessage("assistant", "x", id=uuid.uuid4())])
    assert end_turn(forged) is False
    assert len(messages(db, turn.conversation_id)) == 1
    assert read(db, Conversation, turn.conversation_id)["active_turn_id"] == turn.turn_id


def test_a_conversation_of_someone_else_cannot_be_read(db, stranger):
    conversation = add_conversation(db)
    with pytest.raises(AssistantError) as caught:
        timeline.load(stranger, conversation)
    assert (caught.value.code, caught.value.status) == ("conversation_not_found", 404)


@pytest.mark.parametrize("state", ["pending", "applying", "applied", "expired"])
@pytest.mark.parametrize("act", [drafts_bl.claim, drafts_bl.discard])
def test_a_draft_of_someone_else_is_not_found(db, stranger, act, state):
    """Whatever its state: that it exists must not be told either."""
    draft = add_draft(db, add_conversation(db), state=state)
    before = read(db, Draft, draft)
    with pytest.raises(AssistantError) as caught:
        act(stranger, draft)
    assert (caught.value.code, caught.value.status) == ("draft_not_found", 404)
    assert "draft" not in caught.value.extra
    assert read(db, Draft, draft) == before


def test_a_draft_cannot_be_changed_as_someone_else(db, stranger):
    conversation = add_conversation(db)
    draft = add_draft(db, conversation, state="applying", updated_at=ago(hours=1))
    before = read(db, Draft, draft), read(db, Conversation, conversation)
    forged = drafts_bl.DraftKey(draft, stranger.tenant_id, stranger.email, conversation)
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1"))
    _, outcome = alert_set.pending(payload, {}, "unknown")
    drafts_bl.revert(forged)
    assert drafts_bl.keep_applying(forged, outcome) is None
    assert drafts_bl.claim_look(forged, 30) is None
    resolved = drafts_bl.resolve(
        forged, from_state="applying", state="failed", event="x", outcome=outcome
    )
    assert resolved is None
    with pytest.raises(AssistantError) as caught:
        drafts_bl.load(forged)
    assert caught.value.code == "draft_not_found"
    assert (read(db, Draft, draft), read(db, Conversation, conversation)) == before
    assert messages(db, conversation) == []


def test_drafts_of_someone_else_are_not_shown(db, stranger):
    """Even where a message names one."""
    draft = add_draft(db, add_conversation(db))
    theirs = add_conversation(
        db, tenant_id=stranger.tenant_id, created_by=stranger.email
    )
    add_message(
        db,
        theirs,
        "tool",
        tenant_id=stranger.tenant_id,
        tool_call_id="1",
        draft_id=draft,
    )
    view = timeline.load(stranger, theirs)
    assert [item for item in view.items if item.type == "draft"] == []


def test_every_row_of_a_turn_carries_its_owner(db):
    turn = start_turn(identity(), None, "mine")
    add_messages(turn, [NewMessage("assistant", "x", id=uuid.uuid4())])
    row = read(db, Conversation, turn.conversation_id)
    assert (row["tenant_id"], row["created_by"]) == (TENANT, EMAIL)
    assert {m["tenant_id"] for m in messages(db, turn.conversation_id)} == {TENANT}
