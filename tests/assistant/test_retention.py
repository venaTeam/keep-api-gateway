"""What is older than 90 days is deleted, and nothing else."""
import logging
import threading
import time

import pytest
import sqlalchemy as sa

from src.services.assistant.bl import retention
from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.core.db import get_engine
from src.services.assistant.models.db import Conversation, Draft, Message
from tests.assistant.conftest import OTHER_TENANT
from tests.assistant.logs import rendered
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    add_message,
    ago,
    messages,
    read,
)

CANARY = "canary-d11b70"


@pytest.fixture()
def db(clean_db):
    return clean_db


def run() -> None:
    retention.run(threading.Event())


def count(db, model) -> int:
    with db.connect() as conn:
        return conn.scalar(sa.select(sa.func.count()).select_from(model))


def old_conversation(db, days: float = 91, **values):
    """Last used then."""
    return add_conversation(
        db, created_at=ago(days=days + 1), updated_at=ago(days=days), **values
    )


def used_conversation(db, **values):
    """Old, but used a moment ago."""
    return add_conversation(db, created_at=ago(days=200), updated_at=ago(), **values)


def say(db, conversation, days: float, role="user", **values):
    return add_message(db, conversation, role, created_at=ago(days=days), **values)


def value(metric) -> float:
    return metric._value.get()


# --- conversations nobody uses any more -----------------------------------------------


def test_the_period_is_ninety_days():
    assert get_settings().retention_days == 90


def test_an_idle_conversation_goes_as_a_whole(db):
    idle = old_conversation(db)
    draft = add_draft(db, idle)
    say(db, idle, 91)
    say(db, idle, 91, "tool", tool_call_id="1", draft_id=draft)
    run()
    assert (count(db, Conversation), count(db, Message), count(db, Draft)) == (0, 0, 0)


@pytest.mark.parametrize(("days", "kept"), [(89.9, True), (90.1, False)])
def test_the_boundary(db, days, kept):
    conversation = old_conversation(db, days)
    say(db, conversation, days)
    run()
    assert (read(db, Conversation, conversation) is not None) is kept
    assert count(db, Message) == kept


def test_every_tenant(db):
    """There is one period for all of them."""
    old_conversation(db)
    old_conversation(db, tenant_id=OTHER_TENANT)
    run()
    assert count(db, Conversation) == 0


def test_others_are_not_touched(db):
    old = old_conversation(db)
    say(db, old, 91)
    fresh = add_conversation(db, updated_at=ago(days=10))
    draft = add_draft(db, fresh)
    kept = say(db, fresh, 10)
    before = read(db, Conversation, fresh)
    run()
    assert read(db, Conversation, fresh) == before
    assert [m["id"] for m in messages(db, fresh)] == [kept]
    assert read(db, Draft, draft) is not None


# --- old parts of conversations in use --------------------------------------------------


def test_the_old_part_goes_up_to_a_message_of_the_user(db):
    conversation = used_conversation(db)
    old_draft = add_draft(db, conversation, created_at=ago(days=100))
    say(db, conversation, 100, content="old question")
    say(db, conversation, 100, "assistant", tool_calls=[{"id": "1"}])
    say(db, conversation, 100, "tool", tool_call_id="1", draft_id=old_draft)
    # Answers of the old question, some of them within the period.
    say(db, conversation, 89, "assistant", content="old answer")
    say(db, conversation, 89, "event", content="outcome")
    say(db, conversation, 80, content="new question")
    new_draft = add_draft(db, conversation, created_at=ago(days=80))
    say(db, conversation, 80, "tool", tool_call_id="2", draft_id=new_draft)
    run()
    assert [(m["role"], m["content"]) for m in messages(db, conversation)] == [
        ("user", "new question"),
        ("tool", "some text"),
    ]
    assert read(db, Draft, old_draft) is None
    assert read(db, Draft, new_draft) is not None


def test_the_order_is_by_seq(db):
    """Rows can be older than the one before them: their clock is the transaction's."""
    conversation = used_conversation(db)
    say(db, conversation, 100)
    say(db, conversation, 80, content="new question")
    say(db, conversation, 95, "assistant", content="late")
    run()
    assert [m["content"] for m in messages(db, conversation)] == [
        "new question",
        "late",
    ]


def test_a_conversation_in_use_without_a_recent_question_goes_as_a_whole(db):
    """Say, a Confirm was its last use. Nothing before it may stay."""
    conversation = used_conversation(db)
    draft = add_draft(db, conversation, created_at=ago(days=91))
    say(db, conversation, 91)
    say(db, conversation, 89, "event", draft_id=draft)
    run()
    assert (count(db, Conversation), count(db, Message), count(db, Draft)) == (0, 0, 0)


def test_an_event_about_a_deleted_draft_stays(db):
    conversation = used_conversation(db)
    draft = add_draft(db, conversation, created_at=ago(days=91))
    say(db, conversation, 91)
    say(db, conversation, 89)
    event = say(db, conversation, 89, "event", draft_id=draft)
    run()
    assert read(db, Draft, draft) is None
    assert read(db, Message, event)["draft_id"] is None


def test_a_conversation_without_an_old_question_is_left_alone(db):
    conversation = used_conversation(db)
    kept = [say(db, conversation, 89), say(db, conversation, 1)]
    run()
    assert [m["id"] for m in messages(db, conversation)] == kept


def test_a_conversation_in_use_without_a_question_is_left_alone(db):
    conversation = used_conversation(db)
    kept = say(db, conversation, 100, "event")
    run()
    assert [m["id"] for m in messages(db, conversation)] == [kept]


def test_a_conversation_that_cannot_be_trimmed_does_not_keep_it_busy(
    db, set_env, monkeypatch
):
    """Its times are out of order: it looks the same after every round."""
    set_env("ASSISTANT_RETENTION_BATCH_SIZE", 1)
    monkeypatch.setattr(retention, "MAX_RUN_SECONDS", 10)
    metrics.retention_last_success_unixtime.set(0)
    conversation = used_conversation(db)
    kept = [say(db, conversation, 1), say(db, conversation, 100)]
    run()
    assert value(metrics.retention_last_success_unixtime) > 0
    assert [m["id"] for m in messages(db, conversation)] == kept


def test_trimming_does_not_count_as_use(db):
    conversation = used_conversation(db)
    say(db, conversation, 100)
    say(db, conversation, 1)
    before = read(db, Conversation, conversation)
    run()
    assert len(messages(db, conversation)) == 1
    assert read(db, Conversation, conversation) == before


# --- batches -----------------------------------------------------------------------------


def transactions(db, work) -> list[list[str]]:
    """The statements of every transaction in `work`."""
    found, current = [], []
    engine = get_engine()

    def statement(conn, cursor, text, *args):
        current.append(text)

    def commit(conn):
        found.append(current.copy())
        current.clear()

    def rollback(conn):
        current.clear()

    listeners = {
        "before_cursor_execute": statement,
        "commit": commit,
        "rollback": rollback,
    }
    for name, listener in listeners.items():
        sa.event.listen(engine, name, listener)
    try:
        work()
    finally:
        for name, listener in listeners.items():
            sa.event.remove(engine, name, listener)
    return found


@pytest.mark.parametrize("conversations", [4, 5])
def test_it_goes_on_until_nothing_is_left(db, set_env, conversations):
    set_env("ASSISTANT_RETENTION_BATCH_SIZE", 2)
    for _ in range(conversations):
        old_conversation(db)
        in_use = used_conversation(db)
        say(db, in_use, 100)
        say(db, in_use, 1)
    found = transactions(db, run)
    assert count(db, Conversation) == count(db, Message) == conversations
    with db.connect() as conn:
        assert conn.scalar(sa.select(sa.func.min(Message.created_at))) > ago(days=2)
    # 4: 2 + 2 + none, for each of the passes. 5: 2 + 2 + 1.
    assert len(found) == 6


def test_every_transaction_holds_the_lock_and_has_a_timeout(db):
    old_conversation(db)
    in_use = used_conversation(db)
    say(db, in_use, 100)
    found = transactions(db, run)
    assert len(found) == 2
    for statements in found:
        assert "pg_try_advisory_xact_lock" in statements[0]
        assert statements[1] == "SET LOCAL statement_timeout = '60s'"
        assert "SKIP LOCKED" in statements[2]
        assert len([s for s in statements if s.startswith("DELETE")]) >= 3
    # Neither outlives its transaction.
    with db.connect() as conn:
        assert conn.scalar(sa.text("SHOW statement_timeout")) == "5s"
        assert not conn.scalar(
            sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        )


def test_two_hundred_conversations_at_a_time():
    assert get_settings().retention_batch_size == 200


def test_a_conversation_somebody_works_on_is_left_for_the_next_run(db):
    locked, free = old_conversation(db), old_conversation(db)
    trimmed = used_conversation(db)
    say(db, trimmed, 100)
    say(db, trimmed, 1)
    with db.connect() as conn:
        conn.execute(
            sa.select(Conversation.id)
            .where(Conversation.id.in_([locked, trimmed]))
            .with_for_update()
        )
        started = time.monotonic()
        run()
        assert time.monotonic() - started < 5
        conn.rollback()
    assert read(db, Conversation, free) is None
    assert read(db, Conversation, locked) is not None
    assert len(messages(db, trimmed)) == 2
    run()
    assert read(db, Conversation, locked) is None
    assert len(messages(db, trimmed)) == 1


# --- two workers, failures, the end of a run ------------------------------------------------


def test_the_lock_is_not_the_one_of_the_migrations():
    assert get_settings().retention_lock_key not in (0, 8274419300112233)


def test_another_worker_is_at_it(db, caplog):
    conversation = old_conversation(db)
    skipped = value(metrics.retention_lock_skipped_total)
    success = value(metrics.retention_last_success_unixtime)
    failures = value(metrics.retention_failures_total)
    with db.connect() as conn, caplog.at_level(logging.INFO):
        conn.execute(
            sa.select(sa.func.pg_advisory_xact_lock(get_settings().retention_lock_key))
        )
        run()
        conn.rollback()
    assert read(db, Conversation, conversation) is not None
    assert value(metrics.retention_lock_skipped_total) == skipped + 1
    assert value(metrics.retention_last_success_unixtime) == success
    assert value(metrics.retention_failures_total) == failures
    [line] = [r for r in caplog.records if "another worker" in r.message]
    assert line.levelname == "INFO"


def test_success_is_recorded(db):
    run()
    assert time.time() - value(metrics.retention_last_success_unixtime) < 60


def test_what_was_deleted_is_counted(db):
    def deleted(table):
        return value(metrics.retention_rows_deleted_total.labels(table=table))

    tables = ("assistant_conversation", "assistant_draft", "assistant_message")
    before = [deleted(table) for table in tables]
    idle = old_conversation(db)
    add_draft(db, idle)
    say(db, idle, 91)
    in_use = used_conversation(db)
    say(db, in_use, 100)
    say(db, in_use, 99)
    say(db, in_use, 1)
    run()
    assert [deleted(t) - b for t, b in zip(tables, before)] == [1, 1, 3]


def test_a_failure_is_counted_and_logged_without_its_text(db, monkeypatch, caplog):
    conversation = old_conversation(db)

    def fail(self, session, ids):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(retention._Run, "_delete_conversations", fail)
    failures = value(metrics.retention_failures_total)
    success = value(metrics.retention_last_success_unixtime)
    with caplog.at_level(logging.DEBUG):
        run()
    assert value(metrics.retention_failures_total) == failures + 1
    assert value(metrics.retention_last_success_unixtime) == success
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert read(db, Conversation, conversation) is not None


def test_a_batch_is_deleted_as_a_whole_or_not_at_all(db, monkeypatch):
    conversation = old_conversation(db)
    say(db, conversation, 91)
    real = sa.delete

    def fail(model):
        if model is Conversation:
            raise RuntimeError(CANARY)
        return real(model)

    monkeypatch.setattr(retention.sa, "delete", fail)
    run()
    monkeypatch.undo()
    assert len(messages(db, conversation)) == 1


def test_it_ends_between_batches_when_it_is_told_to(db, set_env, monkeypatch, caplog):
    set_env("ASSISTANT_RETENTION_BATCH_SIZE", 1)
    for _ in range(3):
        old_conversation(db)
    stop = threading.Event()
    real = retention._Run._delete_idle_conversations

    def then_stop(self):
        more = real(self)
        stop.set()
        return more

    monkeypatch.setattr(retention._Run, "_delete_idle_conversations", then_stop)
    success = value(metrics.retention_last_success_unixtime)
    retention.run(stop)
    assert count(db, Conversation) == 2
    assert value(metrics.retention_last_success_unixtime) == success
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_a_run_has_an_end(db, set_env, monkeypatch, caplog):
    assert retention.MAX_RUN_SECONDS == 600
    monkeypatch.setattr(retention, "MAX_RUN_SECONDS", -1)
    old_conversation(db)
    success = value(metrics.retention_last_success_unixtime)
    run()
    assert count(db, Conversation) == 1
    assert value(metrics.retention_last_success_unixtime) == success
    [line] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert line.message == "assistant: retention ran out of time"


def test_told_to_stop_before_it_starts(db):
    stop = threading.Event()
    stop.set()
    old_conversation(db)
    retention.run(stop)
    assert count(db, Conversation) == 1
