import uuid

import pytest
import sqlalchemy as sa

from src.services.assistant.core import db as db_core


@pytest.fixture()
def probe(db_engine, monkeypatch):
    """The name of a scratch table standing in for the required schema. Unique per
    test, so it goes away with the container and nothing has to remove it."""
    name = f"schema_probe_{uuid.uuid4().hex}"
    monkeypatch.setattr(
        db_core, "REQUIRED_SCHEMA", {name: frozenset({"id", "created_by"})}
    )
    return name


@pytest.fixture()
def probe_table(probe, db_engine):
    with db_engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE {probe} (id int, created_by text)"))
    return probe


# --- engine -----------------------------------------------------------------


def test_engine_has_statement_timeout_and_hides_parameters(db_engine):
    with db_engine.connect() as conn:
        assert conn.execute(sa.text("SHOW statement_timeout")).scalar() == "5s"
        with pytest.raises(sa.exc.ProgrammingError) as raised:
            conn.execute(sa.text("SELECT :v FROM no_such_table"), {"v": "private"})
    # Postgres may still quote literals in its own error line, which is why no
    # exception text is ever logged; SQLAlchemy at least adds none.
    assert "[parameters:" not in str(raised.value)
    assert "SQL parameters hidden" in str(raised.value)


def test_check_timeout_does_not_leak_into_the_pool(db_engine, probe_table):
    db_core.check_schema()
    with db_engine.connect() as conn:
        assert conn.execute(sa.text("SHOW statement_timeout")).scalar() == "5s"


# --- schema -----------------------------------------------------------------


def test_a_complete_schema_lacks_nothing(probe_table):
    assert db_core.schema_verified() is False
    assert db_core.check_schema() == []
    assert db_core.schema_verified() is True


def test_missing_column_is_reported_until_it_exists(probe, db_engine):
    with db_engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE {probe} (id int)"))
    assert db_core.check_schema() == [f"{probe}.created_by"]
    # A failure is not cached.
    with db_engine.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE {probe} ADD COLUMN created_by text"))
    assert db_core.check_schema() == []


def test_missing_table_is_reported(probe):
    assert db_core.check_schema() == [probe]
    assert db_core.schema_verified() is False


def test_complete_schema_is_cached(probe_table, monkeypatch):
    assert db_core.check_schema() == []
    monkeypatch.setattr(
        db_core, "_missing_schema", lambda conn: pytest.fail("schema read again")
    )
    assert db_core.check_schema() == []
