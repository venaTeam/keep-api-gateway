import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.services.assistant.bl.conversations_bl import end_turn, start_turn
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import AssistantError
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.fakes import identity
from tests.assistant.rows import add_conversation, add_message, ago, messages

ME = identity()


@pytest.fixture()
def db(clean_db, set_env):
    set_env("ASSISTANT_RATE_LIMIT_PER_MINUTE", 3)
    set_env("ASSISTANT_RATE_LIMIT_PER_DAY", 5)
    return clean_db


def send(who=ME, conversation_id=None) -> None:
    end_turn(start_turn(who, conversation_id, "hello"))


def refused(who=ME, conversation_id=None) -> int:
    before = metrics.rate_limited_total._value.get()
    with pytest.raises(AssistantError) as caught:
        send(who, conversation_id)
    assert caught.value.code == "rate_limited"
    assert metrics.rate_limited_total._value.get() == before + 1
    return int(caught.value.headers["Retry-After"])


def test_messages_per_minute(db):
    for _ in range(3):
        send()
    assert 50 <= refused() <= 60


def test_the_oldest_message_of_the_minute_sets_the_wait(db):
    conversation = add_conversation(db)
    for seconds in (70, 50, 20, 10):
        add_message(db, conversation, created_at=ago(seconds=seconds))
    assert 5 <= refused(conversation_id=conversation) <= 11


def test_messages_older_than_a_minute_do_not_count(db):
    conversation = add_conversation(db)
    for _ in range(3):
        add_message(db, conversation, created_at=ago(seconds=61))
    send(conversation_id=conversation)


def test_messages_per_day(db):
    conversation = add_conversation(db)
    for hours in (23, 20, 10, 5, 1):
        add_message(db, conversation, created_at=ago(hours=hours))
    assert 3500 <= refused() <= 3600


def test_messages_older_than_a_day_do_not_count(db):
    conversation = add_conversation(db)
    for _ in range(5):
        add_message(db, conversation, created_at=ago(hours=25))
    send(conversation_id=conversation)


def test_the_limit_is_per_user_and_tenant(db):
    for _ in range(3):
        send()
    send(identity(email="else@example.com"))
    send(identity(tenant_id=OTHER_TENANT))
    refused()


def test_only_the_users_own_messages_count(db):
    conversation = add_conversation(db)
    for role in ("assistant", "tool", "event"):
        for _ in range(3):
            add_message(db, conversation, role)
    send(conversation_id=conversation)


def test_it_counts_over_all_conversations(db):
    for _ in range(3):
        send()  # each in a new one
    conversation = add_conversation(db)
    refused(conversation_id=conversation)
    assert messages(db, conversation) == []


def test_a_limit_of_zero_refuses_everything(db, set_env):
    set_env("ASSISTANT_RATE_LIMIT_PER_MINUTE", 0)
    assert refused() == 60


def test_simultaneous_messages_stay_within_the_limit(db, set_env):
    set_env("ASSISTANT_RATE_LIMIT_PER_MINUTE", 1)
    together = threading.Barrier(4)

    def attempt(_):
        together.wait(10)
        try:
            start_turn(ME, None, "hello")
            return "started"
        except AssistantError as exc:
            return exc.code

    with ThreadPoolExecutor(4) as pool:
        assert sorted(pool.map(attempt, range(4))) == ["rate_limited"] * 3 + ["started"]
