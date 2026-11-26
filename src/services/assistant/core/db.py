"""The assistant's own small engine on the gateway's database.

Separate from the gateway's pool on purpose: assistant SQL is bounded by its own
statement timeout and cannot use up the connections the UI's requests need.

Everything here blocks: async code calls it through run_in_threadpool, in short
transactions, and never holds a connection across an LLM or gateway call.
"""
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError
from sqlmodel import Session, create_engine

from src.config import consts
from src.services.assistant.config import get_settings

# What the keep-migrations revision `create_assistant_tables` provides. Columns
# too: an older table must show up as `not_ready`, not as failing requests.
REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "assistant_conversation": frozenset(
        {
            "id",
            "tenant_id",
            "created_by",
            "title",
            "active_turn_id",
            "turn_started_at",
            "created_at",
            "updated_at",
        }
    ),
    "assistant_draft": frozenset(
        {
            "id",
            "tenant_id",
            "conversation_id",
            "created_by",
            "kind",
            "state",
            "permitted",
            "payload",
            "outcome",
            "expires_at",
            "resolved_at",
            "created_at",
            "updated_at",
        }
    ),
    "assistant_message": frozenset(
        {
            "id",
            "tenant_id",
            "conversation_id",
            "seq",
            "role",
            "status",
            "content",
            "tool_calls",
            "tool_call_id",
            "draft_id",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "created_at",
        }
    ),
}

_engine: Engine | None = None
_engine_lock = threading.Lock()
_schema_verified = False


class DatabaseNotConfiguredError(Exception):
    """The gateway does not run on a Postgres connection string."""


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:  # first use comes from several threadpool workers
            if _engine is None:
                _engine = _create_engine()
    return _engine


def _create_engine() -> Engine:
    dsn = consts.DB_CONNECTION_STRING
    try:
        is_postgres = bool(dsn) and make_url(dsn).get_backend_name() == "postgresql"
    except ArgumentError:
        is_postgres = False
    if not is_postgres:
        raise DatabaseNotConfiguredError()
    settings = get_settings()
    return create_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        # SQLAlchemy puts bound parameters (message text, alert data) into its
        # exceptions; those must never reach a log.
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )


def dispose_engine() -> None:
    """Close the pool; the next get_engine() builds a new one."""
    global _engine, _schema_verified
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _schema_verified = False


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def schema_verified() -> bool:
    """True once check_schema() has found everything."""
    return _schema_verified


def check_schema() -> list[str]:
    """What the schema lacks (sorted). Raises when the database cannot answer.

    A complete schema is remembered: it cannot become incomplete while this process
    runs without a forbidden downgrade, and this runs before every request.
    """
    global _schema_verified
    if _schema_verified:
        return []
    with get_engine().connect() as conn, conn.begin():
        # SET LOCAL ends with this transaction, so it cannot leak into the pool.
        conn.exec_driver_sql(
            "SET LOCAL statement_timeout = "
            f"{int(get_settings().db_schema_check_timeout_ms)}"
        )
        missing = _missing_schema(conn)
    _schema_verified = not missing
    return missing


def _missing_schema(conn: Connection) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY(:tables)"
        ),
        {"tables": list(REQUIRED_SCHEMA)},
    )
    present: dict[str, set[str]] = {}
    for table, column in rows:
        present.setdefault(table, set()).add(column)
    missing = []
    for table, columns in REQUIRED_SCHEMA.items():
        if table not in present:
            missing.append(table)
        else:
            missing.extend(f"{table}.{name}" for name in columns - present[table])
    return sorted(missing)
