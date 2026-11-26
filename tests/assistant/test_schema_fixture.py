"""The ORM models must match the schema that keep-migrations creates.

tests/fixtures/assistant_schema.json and its dump helper are vendored byte for byte
from keep-migrations, which checks its migration against the same file. This side
reflects the Postgres tables that conftest created from the models.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlmodel import Session, SQLModel, select

from src.services.assistant.core.db import REQUIRED_SCHEMA
from src.services.assistant.models.db import Conversation, Draft, Message
from src.services.assistant.models.db.base import AssistantModel
from tests.assistant.conftest import OTHER_TENANT, TENANT
from tests.fixtures.assistant_schema_dump import ASSISTANT_TABLES, dump_from_inspector

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
GOLDEN = json.loads((FIXTURES / "assistant_schema.json").read_text("utf-8"))["tables"]
SECTIONS = (
    "columns",
    "primary_key",
    "indexes",
    "unique_constraints",
    "foreign_keys",
    "check_constraints",
)
# Blobs of keep-migrations' copies.
VENDORED = {
    "assistant_schema.json": "f600dbaa084daa4c3be0d08d13a6f76eb6b6323e",
    "assistant_schema_dump.py": "4e5fd6e5ccf515344a9155d73bdc8660be5c4f0f",
}


@pytest.fixture()
def model_dump(db_engine):
    return dump_from_inspector(sa.inspect(db_engine))["tables"]


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_vendored_files_are_byte_identical(name):
    result = subprocess.run(
        ["git", "hash-object", "--no-filters", str(FIXTURES / name)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == VENDORED[name]


def test_metadata_holds_exactly_the_fixtured_tables():
    owned = AssistantModel.metadata.tables
    assert sorted(owned) == sorted(ASSISTANT_TABLES) == sorted(GOLDEN)


def test_the_gateways_own_metadata_has_no_assistant_table():
    """It is created on SQLite, and read by the gateway's readiness check."""
    import src.models.db.all_models  # noqa: F401

    assert AssistantModel.metadata is not SQLModel.metadata
    assert "tenant" in SQLModel.metadata.tables
    assert not set(ASSISTANT_TABLES) & set(SQLModel.metadata.tables)


@pytest.mark.parametrize("table", ASSISTANT_TABLES)
@pytest.mark.parametrize("section", SECTIONS)
def test_models_match_the_golden_fixture(model_dump, table, section):
    assert sorted(GOLDEN[table]) == sorted(SECTIONS)
    assert model_dump[table][section] == GOLDEN[table][section]


def test_readiness_requires_exactly_the_model_columns():
    assert REQUIRED_SCHEMA == {
        name: frozenset(column.name for column in table.columns)
        for name, table in AssistantModel.metadata.tables.items()
    }


def test_the_models_need_no_tenant_table_to_write(assistant_dsn):
    """Their MetaData has none; conftest's stand-in must not hide that."""
    code = (
        "from src.services.assistant.core.db import get_session\n"
        "from src.services.assistant.models.db import Conversation, Message\n"
        "assert 'tenant' not in Conversation.metadata.tables\n"
        "with get_session() as s:\n"
        f"    conv = Conversation(tenant_id={TENANT!r}, created_by='x')\n"
        "    s.add(conv); s.flush()\n"
        f"    message = Message(tenant_id={TENANT!r}, role='user')\n"
        "    message.conversation_id = conv.id\n"
        "    s.add(message)\n"
        "    s.flush()\n"
        "    print('written')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_CONNECTION_STRING": assistant_dsn},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "written" in result.stdout


# --- what the models do with those columns -----------------------------------


def add_conversation(session, **fields) -> Conversation:
    conversation = Conversation(
        **{"tenant_id": TENANT, "created_by": "a@example.com", **fields}
    )
    session.add(conversation)
    session.flush()
    return conversation


def make_draft(conversation, **fields) -> Draft:
    return Draft(
        **{
            "tenant_id": conversation.tenant_id,
            "conversation_id": conversation.id,
            "created_by": conversation.created_by,
            "kind": "change_alert_status",
            "payload": {"a": 1},
            "expires_at": datetime.now(timezone.utc),
            **fields,
        }
    )


def make_message(conversation, **fields) -> Message:
    return Message(
        **{
            "tenant_id": conversation.tenant_id,
            "conversation_id": conversation.id,
            "role": "user",
            **fields,
        }
    )


def test_seq_increases_in_insert_order_within_one_transaction(assistant_session):
    conversation = add_conversation(assistant_session)
    messages = [make_message(conversation, content=str(n)) for n in range(5)]
    for message in messages:
        assistant_session.add(message)
        assistant_session.flush()
    seqs = [message.seq for message in messages]
    assert seqs == sorted(set(seqs))
    assert len({message.created_at for message in messages}) == 1
    assert messages[0].created_at.tzinfo is not None


def test_none_is_stored_as_sql_null(assistant_session):
    conversation = add_conversation(assistant_session)
    draft = make_draft(conversation)
    assistant_session.add(draft)
    assistant_session.flush()
    message = make_message(conversation, draft_id=draft.id)
    assistant_session.add(message)
    assistant_session.flush()
    for table, column, row_id in (
        ("assistant_draft", "outcome", draft.id),
        ("assistant_message", "tool_calls", message.id),
    ):
        is_null = assistant_session.execute(
            sa.text(f"SELECT {column} IS NULL FROM {table} WHERE id = :id"),
            {"id": row_id},
        ).scalar_one()
        assert is_null, column


def test_deleting_a_draft_clears_the_reference(assistant_session):
    conversation = add_conversation(assistant_session)
    draft = make_draft(conversation)
    assistant_session.add(draft)
    assistant_session.flush()
    message = make_message(conversation, draft_id=draft.id, role="tool")
    assistant_session.add(message)
    assistant_session.flush()
    assistant_session.execute(sa.delete(Draft).where(Draft.id == draft.id))
    assistant_session.expire_all()
    stored = assistant_session.exec(select(Message.draft_id)).all()
    assert stored == [None]


@pytest.mark.parametrize(
    "build",
    [
        lambda c: make_draft(c, state="done"),
        lambda c: make_message(c, role="system"),
        lambda c: make_message(c, status="half"),
        # A child of another tenant than its conversation's.
        lambda c: make_draft(c, tenant_id=OTHER_TENANT),
        lambda c: make_message(c, tenant_id=OTHER_TENANT),
        lambda c: make_message(c, conversation_id=uuid4()),
        lambda c: make_message(c, tenant_id="no-such-tenant"),
    ],
)
def test_the_database_rejects(assistant_session: Session, build):
    conversation = add_conversation(assistant_session)
    assistant_session.add(build(conversation))
    with pytest.raises(sa.exc.IntegrityError):
        assistant_session.flush()
