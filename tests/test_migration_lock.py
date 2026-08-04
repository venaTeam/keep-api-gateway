"""
Tests for serializing `alembic upgrade head` across gateway replicas.

All 5 replicas migrate in-process on startup. Without a lock they read the same
old revision and attempt the same DDL; the losers fail with "already exists",
which propagates out of gunicorn's `on_starting` and CrashLoopBackOffs the pod.
"""

from unittest.mock import MagicMock, patch

from src.repositories import db_on_start


def _engine(dialect="postgresql"):
    engine = MagicMock()
    engine.dialect.name = dialect
    return engine


def _conn_returning(*try_lock_results):
    """A connection whose pg_try_advisory_lock returns the given sequence."""
    conn = MagicMock()
    results = list(try_lock_results)

    def execute(statement, params=None):
        result = MagicMock()
        text = str(statement)
        if "pg_try_advisory_lock" in text:
            result.scalar.return_value = results.pop(0)
        else:
            result.scalar.return_value = True
        return result

    conn.execute.side_effect = execute
    return conn


def _executed_sql(conn):
    return [str(call.args[0]) for call in conn.execute.call_args_list]


def test_migration_runs_under_the_advisory_lock(monkeypatch):
    conn = _conn_returning(True)
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(db_on_start, "engine", engine)
    monkeypatch.delenv("SKIP_DB_CREATION", raising=False)

    with patch("alembic.command.upgrade") as upgrade:
        db_on_start.migrate_db()

    upgrade.assert_called_once()
    sql = _executed_sql(conn)
    assert any("pg_try_advisory_lock" in s for s in sql)
    assert any("pg_advisory_unlock" in s for s in sql)
    conn.close.assert_called_once()


def test_lock_is_released_even_if_the_migration_fails(monkeypatch):
    conn = _conn_returning(True)
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(db_on_start, "engine", engine)
    monkeypatch.delenv("SKIP_DB_CREATION", raising=False)

    with patch("alembic.command.upgrade", side_effect=RuntimeError("bad migration")):
        try:
            db_on_start.migrate_db()
        except RuntimeError:
            pass

    assert any("pg_advisory_unlock" in s for s in _executed_sql(conn))
    conn.close.assert_called_once()


def test_second_replica_waits_for_the_lock(monkeypatch):
    """The loser polls instead of racing the DDL; its own `upgrade head` is then
    a no-op."""
    conn = _conn_returning(False, False, True)
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(db_on_start, "engine", engine)
    monkeypatch.setattr(db_on_start, "_MIGRATION_LOCK_POLL_SECONDS", 0)
    monkeypatch.delenv("SKIP_DB_CREATION", raising=False)

    with patch("alembic.command.upgrade") as upgrade:
        db_on_start.migrate_db()

    attempts = [s for s in _executed_sql(conn) if "pg_try_advisory_lock" in s]
    assert len(attempts) == 3
    upgrade.assert_called_once()


def test_lock_wait_is_bounded(monkeypatch):
    """A stuck migrator must not hang pod startup forever: after the timeout we
    proceed without the lock rather than block."""
    conn = _conn_returning(*([False] * 50))
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(db_on_start, "engine", engine)
    monkeypatch.setattr(db_on_start, "_MIGRATION_LOCK_POLL_SECONDS", 0)
    monkeypatch.setattr(db_on_start, "_MIGRATION_LOCK_TIMEOUT", 0)
    monkeypatch.delenv("SKIP_DB_CREATION", raising=False)

    with patch("alembic.command.upgrade") as upgrade:
        db_on_start.migrate_db()

    upgrade.assert_called_once()
    assert not any("pg_advisory_unlock" in s for s in _executed_sql(conn))


def test_non_postgres_dialect_skips_the_lock(monkeypatch):
    engine = _engine(dialect="sqlite")
    monkeypatch.setattr(db_on_start, "engine", engine)
    monkeypatch.delenv("SKIP_DB_CREATION", raising=False)

    with patch("alembic.command.upgrade") as upgrade:
        db_on_start.migrate_db()

    upgrade.assert_called_once()
    engine.connect.assert_not_called()


def test_skip_db_creation_short_circuits(monkeypatch):
    monkeypatch.setenv("SKIP_DB_CREATION", "true")
    with patch("alembic.command.upgrade") as upgrade:
        assert db_on_start.migrate_db() is None
    upgrade.assert_not_called()


def test_schema_at_head_compares_db_to_script_head(monkeypatch):
    with patch.object(db_on_start, "get_db_revision", return_value="rev1"):
        with patch.object(db_on_start, "get_script_head", return_value="rev1"):
            assert db_on_start.schema_at_head() == (True, "rev1", "rev1")

        with patch.object(db_on_start, "get_script_head", return_value="rev2"):
            at_head, db_rev, head = db_on_start.schema_at_head()
            assert (at_head, db_rev, head) == (False, "rev1", "rev2")

    # No alembic_version table at all: not at head.
    with patch.object(db_on_start, "get_db_revision", return_value=None):
        with patch.object(db_on_start, "get_script_head", return_value="rev1"):
            assert db_on_start.schema_at_head()[0] is False


def test_script_head_is_readable_from_the_shipped_migrations():
    """Guards the absolute script_location: a broken path would make /readyz
    permanently report "not at head"."""
    assert db_on_start.get_script_head()
