"""One turn per conversation, two per user, and no writes by a turn that lost its
conversation."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from src.services.assistant.bl import conversations_bl
from src.services.assistant.bl.conversations_bl import (
    TURN_STALE_SECONDS,
    NewMessage,
    TurnLost,
    add_messages,
    end_turn,
    start_turn,
)
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.db import Conversation, Draft, Message
from src.services.assistant.tools.base import NewDraft
from src.services.assistant.models.api.draft import ChangeAlertStatusPayload
from tests.assistant.conftest import TENANT
from tests.assistant.fakes import identity
from tests.assistant.rows import ago, draft_payload, messages, read

ME = identity()


@pytest.fixture()
def db(clean_db):
    return clean_db


def refused(conversation_id, code, who=ME) -> AssistantError:
    with pytest.raises(AssistantError) as caught:
        start_turn(who, conversation_id, "again")
    assert caught.value.code == code
    return caught.value


def answer(text: str, **values) -> NewMessage:
    return NewMessage("assistant", text, id=uuid.uuid4(), **values)


def make_stale(db, conversation_id) -> None:
    with db.begin() as conn:
        conn.execute(
            sa.update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(turn_started_at=ago(seconds=TURN_STALE_SECONDS + 1))
        )


# --- starting ----------------------------------------------------------------


def test_the_first_message_makes_a_conversation(db):
    turn = start_turn(ME, None, "  ack   the\nred ones ")
    row = read(db, Conversation, turn.conversation_id)
    assert (row["tenant_id"], row["created_by"]) == (TENANT, ME.email)
    assert row["title"] == "ack the red ones"
    assert row["active_turn_id"] == turn.turn_id
    assert row["turn_started_at"] is not None
    [message] = messages(db, turn.conversation_id)
    assert message["id"] == turn.user_message_id
    assert (message["role"], message["status"]) == ("user", "complete")
    assert message["content"] == "  ack   the\nred ones "


def test_a_long_first_message_is_cut_for_the_title(db):
    turn = start_turn(ME, None, "x" * 4000)
    assert read(db, Conversation, turn.conversation_id)["title"] == "x" * 120


def test_a_next_message_goes_into_the_same_conversation(db):
    first = start_turn(ME, None, "one")
    before = read(db, Conversation, first.conversation_id)
    assert end_turn(first)
    second = start_turn(ME, first.conversation_id, "two")
    assert second.conversation_id == first.conversation_id
    assert second.turn_id != first.turn_id
    after = read(db, Conversation, first.conversation_id)
    assert after["title"] == "one"
    assert after["updated_at"] > before["updated_at"]
    assert [m["content"] for m in messages(db, first.conversation_id)] == ["one", "two"]


def test_an_unknown_conversation_is_not_found_and_not_made(db):
    unknown = uuid.uuid4()
    refused(unknown, "conversation_not_found")
    assert read(db, Conversation, unknown) is None


# --- one per conversation ------------------------------------------------------


def test_a_second_turn_of_a_conversation_is_refused(db):
    turn = start_turn(ME, None, "one")
    before = metrics.turn_lock_conflicts_total._value.get()
    refused(turn.conversation_id, "turn_in_progress")
    assert metrics.turn_lock_conflicts_total._value.get() == before + 1
    assert len(messages(db, turn.conversation_id)) == 1
    assert read(db, Conversation, turn.conversation_id)["active_turn_id"] == turn.turn_id


def test_a_stale_lock_can_be_taken_over(db):
    lost = start_turn(ME, None, "one")
    make_stale(db, lost.conversation_id)
    turn = start_turn(ME, lost.conversation_id, "two")
    assert read(db, Conversation, turn.conversation_id)["active_turn_id"] == turn.turn_id


def test_simultaneous_messages_make_one_turn(db):
    turn = start_turn(ME, None, "one")
    end_turn(turn)
    together = threading.Barrier(4)

    def attempt(_):
        together.wait(10)
        try:
            return start_turn(ME, turn.conversation_id, "again").turn_id
        except AssistantError as exc:
            return exc.code

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sorted(r for r in results if isinstance(r, str)) == ["turn_in_progress"] * 3
    assert len(messages(db, turn.conversation_id)) == 2


# --- two per user ----------------------------------------------------------------


def test_a_third_turn_at_once_is_refused(db):
    first = start_turn(ME, None, "one")
    start_turn(ME, None, "two")
    before = metrics.rate_limited_total._value.get()
    error = refused(None, "too_many_active_turns")
    assert error.headers == {"Retry-After": "5"}
    assert metrics.rate_limited_total._value.get() == before + 1
    # Other people's turns are no concern of this user.
    start_turn(identity(email="else@example.com"), None, "hello")
    end_turn(first)
    start_turn(ME, None, "three")


def test_a_stale_turn_does_not_count(db):
    for text in ("one", "two"):
        make_stale(db, start_turn(ME, None, text).conversation_id)
    start_turn(ME, None, "three")


def test_the_running_conversation_wins_over_the_count(db):
    first = start_turn(ME, None, "one")
    start_turn(ME, None, "two")
    refused(first.conversation_id, "turn_in_progress")


def test_simultaneous_new_conversations_stay_within_the_limit(db):
    together = threading.Barrier(4)

    def attempt(number):
        together.wait(10)
        try:
            start_turn(ME, None, f"message {number}")
            return "started"
        except AssistantError as exc:
            return exc.code

    with ThreadPoolExecutor(4) as pool:
        results = sorted(pool.map(attempt, range(4)))
    assert results == ["started"] * 2 + ["too_many_active_turns"] * 2


# --- writes of a turn ----------------------------------------------------------


def test_rows_keep_the_order_they_were_given_in(db):
    turn = start_turn(ME, None, "one")
    calls = [{"id": "call-1", "name": "t", "arguments": "{}"}]
    add_messages(
        turn,
        [
            answer("", tool_calls=calls, model="m", prompt_tokens=5, completion_tokens=2),
            NewMessage("tool", "{}", id=uuid.uuid4(), tool_call_id="call-1"),
            answer("done"),
        ],
    )
    rows = messages(db, turn.conversation_id)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[1]["tool_calls"] == calls
    assert (rows[1]["model"], rows[1]["prompt_tokens"]) == ("m", 5)
    assert rows[1]["completion_tokens"] == 2
    assert rows[2]["tool_call_id"] == "call-1"


def test_model_output_that_postgres_cannot_hold_is_stored_without_it(db):
    turn = start_turn(ME, None, "one")
    bad = "a\x00b\ud800c"
    add_messages(
        turn,
        [
            answer(bad, tool_calls=[{"id": bad, "name": bad, "arguments": bad}]),
            NewMessage("tool", bad, id=uuid.uuid4(), tool_call_id=bad),
        ],
    )
    _, assistant, tool = messages(db, turn.conversation_id)
    assert assistant["content"] == tool["content"] == tool["tool_call_id"] == "ab?c"
    assert assistant["tool_calls"] == [{"id": "ab?c", "name": "ab?c", "arguments": "ab?c"}]


def new_draft() -> NewDraft:
    return NewDraft(
        id=uuid.uuid4(),
        kind="change_alert_status",
        permitted=True,
        payload=ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1")),
    )


def test_a_draft_and_its_message_are_stored_together(db):
    turn = start_turn(ME, None, "one")
    draft = new_draft()
    view = add_messages(
        turn,
        [NewMessage("tool", "{}", id=uuid.uuid4(), draft_id=draft.id)],
        draft=draft,
    )
    assert (view.id, view.state) == (draft.id, "pending")
    row = read(db, Draft, draft.id)
    assert (row["tenant_id"], row["created_by"]) == (TENANT, ME.email)
    assert row["conversation_id"] == turn.conversation_id
    assert messages(db, turn.conversation_id)[-1]["draft_id"] == draft.id


def test_every_write_bumps_the_conversation(db):
    turn = start_turn(ME, None, "one")
    for write in (lambda: add_messages(turn, [answer("x")]), lambda: end_turn(turn)):
        before = read(db, Conversation, turn.conversation_id)["updated_at"]
        write()
        assert read(db, Conversation, turn.conversation_id)["updated_at"] > before


def test_ending_releases_the_lock_and_stores_the_partial_answer(db):
    turn = start_turn(ME, None, "one")
    assert end_turn(turn, answer("half an ans", status="interrupted")) is True
    row = read(db, Conversation, turn.conversation_id)
    assert (row["active_turn_id"], row["turn_started_at"]) == (None, None)
    assert messages(db, turn.conversation_id)[-1]["status"] == "interrupted"


@pytest.mark.parametrize("how", ["ended", "taken over", "deleted"])
def test_a_turn_that_lost_its_conversation_writes_nothing(db, how):
    turn = start_turn(ME, None, "one")
    if how == "ended":
        end_turn(turn)
    elif how == "taken over":
        make_stale(db, turn.conversation_id)
        start_turn(ME, turn.conversation_id, "two")
    else:
        with db.begin() as conn:
            conn.execute(sa.delete(Message))
            conn.execute(sa.delete(Conversation))
    before = messages(db, turn.conversation_id)
    lock = read(db, Conversation, turn.conversation_id)
    draft = new_draft()
    with pytest.raises(TurnLost):
        add_messages(turn, [answer("late")], draft=draft)
    assert end_turn(turn, answer("later")) is False
    assert messages(db, turn.conversation_id) == before
    assert read(db, Draft, draft.id) is None
    assert read(db, Conversation, turn.conversation_id) == lock


def test_a_failing_insert_leaves_nothing_behind(db, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(conversations_bl, "_insert", fail)
    with pytest.raises(RuntimeError):
        start_turn(ME, None, "one")
    with db.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(Conversation)) == 0
