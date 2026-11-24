"""Rows belong to (tenant, email). For anyone else they do not exist."""
import dataclasses
import uuid

import pytest

from src.services.assistant.bl.conversations_bl import (
    NewMessage,
    TurnLost,
    add_messages,
    end_turn,
    start_turn,
)
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.db import Conversation
from tests.assistant.conftest import OTHER_TENANT, TENANT
from tests.assistant.fakes import identity
from tests.assistant.rows import EMAIL, add_conversation, messages, read

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


def test_every_row_of_a_turn_carries_its_owner(db):
    turn = start_turn(identity(), None, "mine")
    add_messages(turn, [NewMessage("assistant", "x", id=uuid.uuid4())])
    row = read(db, Conversation, turn.conversation_id)
    assert (row["tenant_id"], row["created_by"]) == (TENANT, EMAIL)
    assert {m["tenant_id"] for m in messages(db, turn.conversation_id)} == {TENANT}
