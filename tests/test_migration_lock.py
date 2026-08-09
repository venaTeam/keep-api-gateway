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


def _fake_script_directory(ancestry):
    """A ScriptDirectory stand-in. `ancestry` maps a revision to those it
    descends from, itself included; an absent revision is one this image's
    scripts have never heard of, which is what alembic raises on."""
    script = MagicMock()

    def iterate_revisions(upper, lower):
        if upper not in ancestry:
            raise Exception(f"Can't locate revision identified by '{upper}'")
        return [MagicMock(revision=rev) for rev in ancestry[upper]]

    script.iterate_revisions.side_effect = iterate_revisions
    return script


def _schema_at_head(db_revision, script_head, ancestry=None, strict=False):
    with patch.object(db_on_start, "get_db_revision", return_value=db_revision), patch.object(
        db_on_start, "get_script_head", return_value=script_head
    ), patch.object(
        db_on_start, "_script_directory", return_value=_fake_script_directory(ancestry or {})
    ), patch.object(
        db_on_start, "SCHEMA_STRICT", strict
    ):
        return db_on_start.schema_at_head()


def test_schema_at_head_when_revisions_match():
    assert _schema_at_head("rev2", "rev2") == (True, "rev2", "rev2")


def test_schema_at_head_when_db_is_ahead_of_this_image():
    """A newer replica already migrated — every rolling deploy that carries a
    migration passes through this, and the older pods must stay startable."""
    at_head, _, _ = _schema_at_head("rev3", "rev2", ancestry={"rev3": ["rev3", "rev2", "rev1"]})
    assert at_head is True


def test_schema_at_head_when_db_is_behind_this_image():
    """The migration hasn't run yet — the one case that must fail."""
    at_head, _, _ = _schema_at_head("rev1", "rev3", ancestry={"rev1": ["rev1"]})
    assert at_head is False


def test_schema_at_head_when_db_revision_is_unknown_to_this_image():
    """Image rollback. Under exact equality this failed the startupProbe on every
    pod, so pinning the previous tag CrashLooped the whole gateway."""
    at_head, _, _ = _schema_at_head("rev9", "rev2", ancestry={})
    assert at_head is True


def test_schema_strict_restores_exact_equality():
    at_head, _, _ = _schema_at_head(
        "rev3", "rev2", ancestry={"rev3": ["rev3", "rev2"]}, strict=True
    )
    assert at_head is False


def test_schema_at_head_without_alembic_version_table():
    assert _schema_at_head(None, "rev1")[0] is False


def test_schema_at_head_when_script_head_is_unreadable():
    """No basis to judge, so don't advertise the pod as ready."""
    assert _schema_at_head("rev1", None)[0] is False


def test_script_head_is_readable_from_the_shipped_migrations():
    """Guards the absolute script_location: a broken path would make /readyz
    permanently report "not at head"."""
    assert db_on_start.get_script_head()
