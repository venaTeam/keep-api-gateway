"""No exception text in any log or body, whatever goes wrong.

Each scratch route fails with the canary in its exception text. Without the route
class the gateway's catch-all would log and return that text; the routes set
`request.state.trace_id`, which it needs for that, so that the leak is a real one.
"""
import logging

import pytest
import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.engine.url import make_url

from src.config import consts
from src.services.assistant.api.route import AssistantRoute
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import ERRORS, error_body
from tests.assistant.fakes import TOKEN
from tests.assistant.logs import rendered

CANARY = "canary-9f2c7d"
INTERNAL = error_body("internal", ERRORS["internal"].message)
NOT_READY = error_body("not_ready", ERRORS["not_ready"].message)


class Answer(BaseModel):
    count: int


def set_trace_id(request: Request):
    request.state.trace_id = "trace-1"


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    leaky = {"dependencies": [Depends(set_trace_id)]}

    @assistant_router.get("/scratch/bug", **leaky)
    async def bug():
        raise RuntimeError(CANARY)

    @assistant_router.get("/scratch/bug-in-a-thread", **leaky)
    def bug_in_a_thread():
        raise KeyError(CANARY)

    @assistant_router.get("/scratch/chained-bug", **leaky)
    async def chained_bug():
        try:
            raise ValueError(CANARY)
        except ValueError as exc:
            raise RuntimeError("x") from exc

    async def failing():
        raise RuntimeError(CANARY)

    @assistant_router.get("/scratch/bug-in-a-dependency", **leaky)
    async def bug_in_a_dependency(value=Depends(failing)):
        return {}

    @assistant_router.get("/scratch/bad-answer", response_model=Answer, **leaky)
    async def bad_answer():
        return {"count": CANARY}

    @assistant_router.get("/scratch/http-error", **leaky)
    async def http_error():
        raise HTTPException(500, CANARY)

    @assistant_router.get("/scratch/query", **leaky)
    def query():
        with db_core.get_session() as session:
            session.execute(sa.text("SELECT 1"))
        return {}

    @assistant_router.get("/scratch/slow-query", **leaky)
    def slow_query():
        with db_core.get_session() as session:
            session.execute(sa.text(f"SELECT pg_sleep(30), '{CANARY}'"))

    @assistant_router.get("/scratch/bad-query", **leaky)
    def bad_query():
        with db_core.get_session() as session:
            session.execute(sa.text("SELECT CAST(:v AS int)"), {"v": CANARY})

    return make_app()


@pytest.fixture()
def client(app):
    return TestClient(
        app, headers={"Authorization": TOKEN}, raise_server_exceptions=False
    )


@pytest.fixture()
def records(monkeypatch, caplog):
    """Every record of every logger, propagating or not, at any level."""
    caplog.set_level(logging.DEBUG)
    captured = []
    handle = logging.Logger.handle

    def capture(self, record):
        captured.append(record)
        handle(self, record)

    monkeypatch.setattr(logging.Logger, "handle", capture)
    return captured


def count(site: str) -> float:
    return metrics.internal_errors_total.labels(site=site)._value.get()


def assert_hidden(response, records, *texts):
    assert records
    for text in texts:
        assert text not in response.text
        assert not [r.name for r in records if text in rendered(r)]


def unhandled(records) -> list[logging.LogRecord]:
    return [
        r for r in records if r.getMessage() == "assistant: unhandled request error"
    ]


def test_the_scratch_routes_have_the_route_class(app):
    scratch = [r for r in app.routes if r.path.startswith("/assistant/scratch/")]
    assert len(scratch) == 9
    assert all(isinstance(route, AssistantRoute) for route in scratch)


# --- bugs ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "error_type", "frame"),
    [
        ("bug", "RuntimeError", "bug"),
        ("bug-in-a-thread", "KeyError", "bug_in_a_thread"),
        ("chained-bug", "RuntimeError", "chained_bug"),
        ("bug-in-a-dependency", "RuntimeError", "failing"),
        ("bad-answer", "ResponseValidationError", "serialize_response"),
    ],
)
def test_a_raised_bug(client, schema_ready, records, path, error_type, frame):
    before = count("request")
    response = client.get(f"/assistant/scratch/{path}")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert_hidden(response, records, CANARY)
    (record,) = unhandled(records)
    assert record.error_type == error_type
    assert record.frames[-1].endswith(f" {frame}")
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.exc_info is None
    assert count("request") == before + 1


def test_the_text_of_an_http_error(client, schema_ready, records):
    response = client.get("/assistant/scratch/http-error")
    assert response.status_code == 500
    assert response.json() == error_body("http_error", ERRORS["http_error"].message)
    assert_hidden(response, records, CANARY)


# --- the database --------------------------------------------------------------


def test_a_statement_timeout(client, db_engine, set_env, records):
    set_env("ASSISTANT_DB_STATEMENT_TIMEOUT_MS", 300)
    db_core.dispose_engine()
    response = client.get("/assistant/scratch/slow-query")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert_hidden(response, records, CANARY, "statement timeout")
    (record,) = unhandled(records)
    assert record.error_type == "OperationalError"


def test_a_rejected_value(client, db_engine, records):
    response = client.get("/assistant/scratch/bad-query")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    # Postgres quotes the value in its own error line.
    assert_hidden(response, records, CANARY)
    (record,) = unhandled(records)
    assert record.error_type == "DataError"


@pytest.fixture()
def one_connection(db_engine, set_env):
    for name, value in (("SIZE", 1), ("TIMEOUT", 0.2)):
        set_env(f"ASSISTANT_DB_POOL_{name}", value)
    set_env("ASSISTANT_DB_MAX_OVERFLOW", 0)
    db_core.dispose_engine()


def test_an_exhausted_pool(client, one_connection, records):
    assert client.get("/assistant/scratch/query").status_code == 200
    with db_core.get_engine().connect():
        response = client.get("/assistant/scratch/query")
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert_hidden(response, records, "QueuePool")
    (record,) = unhandled(records)
    assert record.error_type == "TimeoutError"


def test_an_exhausted_pool_during_the_schema_check(client, one_connection, records):
    before = count("readiness_database")
    with db_core.get_engine().connect():
        response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert_hidden(response, records, "QueuePool")
    assert count("readiness_database") == before + 1
    # A failure is not cached.
    assert client.get("/assistant/scratch/query").status_code == 200


def test_a_failed_connection(client, assistant_dsn, monkeypatch, records):
    dsn = make_url(assistant_dsn).set(database=CANARY, password="secret-password")
    monkeypatch.setattr(
        consts, "DB_CONNECTION_STRING", dsn.render_as_string(hide_password=False)
    )
    before = count("readiness_database")
    response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert_hidden(response, records, CANARY, "secret-password")
    assert count("readiness_database") == before + 1


def test_no_database_at_all(client, monkeypatch, records):
    monkeypatch.setattr(consts, "DB_CONNECTION_STRING", f"sqlite:///{CANARY}.db")
    response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert_hidden(response, records, CANARY)


def test_a_missing_table_is_not_ready(client, db_engine, monkeypatch, records):
    monkeypatch.setattr(
        db_core,
        "REQUIRED_SCHEMA",
        {**db_core.REQUIRED_SCHEMA, "assistant_absent": frozenset({"id"})},
    )
    before = count("readiness_schema")
    response = client.get("/assistant/scratch/query")
    assert response.status_code == 503
    assert response.json() == NOT_READY
    assert count("readiness_schema") == before + 1
    (record,) = [r for r in records if hasattr(r, "missing")]
    assert record.missing == ["assistant_absent"]
