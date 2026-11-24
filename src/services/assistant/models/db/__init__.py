"""ORM models of the assistant tables. Importing this registers them all.

keep-migrations (`create_assistant_tables`) creates the tables; these models only
have to agree with it, which tests/test_schema_fixture.py checks on Postgres.

The gateway's `tenant` is not in this MetaData (base.py). Nothing at runtime
resolves our foreign keys to it; what emits DDL (the test harness) adds a stand-in.

There are no relationships, so the order of a flush follows the mapper names:
flush a parent row before adding its children.
"""
from src.services.assistant.models.db.conversation import Conversation
from src.services.assistant.models.db.draft import Draft
from src.services.assistant.models.db.message import Message

__all__ = ["Conversation", "Draft", "Message"]
