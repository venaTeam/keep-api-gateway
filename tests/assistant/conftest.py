"""Fixtures of the assistant suites.

Assistant SQL needs a real Postgres (advisory locks, jsonb, identity), while the
gateway's suite runs on SQLite. So these tests have their own container, holding only
the assistant tables and a stand-in for `tenant`. The fixtures have names of their
own: the root conftest's `db_session` (the gateway's SQLite) stays available.
"""
import dataclasses
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import psycopg2
import pytest
import sqlalchemy as sa

from src.config import consts
from src.services.assistant.config import ENABLED_ENV_VAR, Settings, reset_settings
from src.services.assistant.core import db as db_core
from src.services.assistant.models.db import Conversation, Draft, Message
from src.services.assistant.models.db.base import AssistantModel

# Nothing listens there: a test that does not ask for `db_engine` reaches no database.
NO_DATABASE_DSN = "postgresql://assistant_test:assistant_test@127.0.0.1:1/assistant_test"
TEST_OPENAI_KEY = "test-key-not-real"
TENANT = "keep"
OTHER_TENANT = "other-tenant"

_COMPOSE_FILE = Path(__file__).parent / "docker-compose.test.yml"
_STARTUP_SECONDS = 300


# --- environment ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def assistant_environment(monkeypatch):
    """The developer's .env (real OpenAI key, real database) never reaches a test.

    Set to "" instead of deleted: a later load_dotenv() would put a deleted one back.
    """
    for setting in dataclasses.fields(Settings):
        monkeypatch.setenv(setting.metadata["env"], "")
    monkeypatch.setenv(ENABLED_ENV_VAR, "")
    monkeypatch.setenv("OPENAI_LOG", "")
    monkeypatch.setenv("OPENAI_API_KEY", TEST_OPENAI_KEY)
    monkeypatch.setattr(consts, "DB_CONNECTION_STRING", NO_DATABASE_DSN)
    reset_settings()
    yield
    reset_settings()
    db_core.dispose_engine()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    send = httpx.AsyncHTTPTransport.handle_async_request

    async def local_only(self, request):
        # No host at all is httpx's own UnsupportedProtocol, not a call.
        if request.url.host not in ("", "127.0.0.1", "localhost"):
            pytest.fail(f"a test tried to reach {request.url.host}")
        return await send(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", local_only)


@pytest.fixture()
def set_env(monkeypatch):
    def set_(name: str, value: object) -> None:
        monkeypatch.setenv(name, str(value))
        reset_settings()

    return set_


# --- database (opt-in: only tests that ask for it need Docker) -------------------


@pytest.fixture(scope="session")
def _assistant_postgres():
    """The DSN of this run's own container. Project and port are unique to the run,
    so overlapping runs cannot share, stop or remove each other's."""
    project = f"gateway-assistant-tests-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def compose(*arguments: str) -> str:
        result = subprocess.run(
            ["docker", "compose", "-p", project, "-f", str(_COMPOSE_FILE), *arguments],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            pytest.fail(
                f"docker compose {arguments[0]} failed ({result.returncode}):\n"
                f"{result.stderr}"
            )
        return result.stdout

    try:
        compose("up", "--detach")
        address = compose("port", "postgres", "5432").strip()
        dsn = f"postgresql://assistant_test:assistant_test@{address}/assistant_test"
        _wait_until_ready(dsn)
        yield dsn
    finally:
        compose("down", "--volumes")


def _wait_until_ready(dsn: str) -> None:
    # From here rather than with a container healthcheck: on a slow Docker every
    # `docker exec` of one takes longer than the server needs to start.
    deadline = time.monotonic() + _STARTUP_SECONDS
    while True:
        try:
            psycopg2.connect(dsn, connect_timeout=5).close()
            return
        except psycopg2.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="session")
def _assistant_database(_assistant_postgres):
    # Copies, so that the stand-in for the gateway's `tenant` never gets into the
    # models' own metadata.
    ddl = sa.MetaData()
    tenant = sa.Table(
        "tenant",
        ddl,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
    )
    for table in AssistantModel.metadata.tables.values():
        table.to_metadata(ddl)
    engine = sa.create_engine(_assistant_postgres)
    ddl.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(tenant),
            [{"id": id_, "name": id_} for id_ in (TENANT, OTHER_TENANT)],
        )
    engine.dispose()
    return _assistant_postgres


@pytest.fixture()
def assistant_dsn(_assistant_database):
    return _assistant_database


@pytest.fixture()
def db_engine(assistant_dsn, monkeypatch):
    """The application's own engine, so its pool and timeouts are what is tested."""
    monkeypatch.setattr(consts, "DB_CONNECTION_STRING", assistant_dsn)
    return db_core.get_engine()


@pytest.fixture()
def clean_db(db_engine):
    """The engine, with no assistant rows left of the seeded tenants."""
    with db_engine.begin() as conn:
        for model in (Message, Draft, Conversation):
            conn.execute(
                sa.delete(model).where(model.tenant_id.in_([TENANT, OTHER_TENANT]))
            )
    return db_engine


@pytest.fixture()
def assistant_session(clean_db):
    """A session whose changes are rolled back."""
    with db_core.get_session() as session:
        yield session
        session.rollback()
