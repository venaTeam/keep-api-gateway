"""The automation DDL in this repo must match the golden schema fixture.

The automation tables are split across two repos: the migration that creates
them lives here (``create_automation_tables``), the ORM models that read and
write them live in keep-automation-api (``src/models/db/``). Nothing runs both,
so nothing checked that they agree — a stray column added to the models, or an
index renamed here, left both suites fully green.

``tests/fixtures/automation_schema.json`` is the shared reference that closes
that gap. It is vendored byte-identically into keep-automation-api, which
asserts its ORM models against its own copy; this file asserts the migration
against this copy. Neither repo's CI checks out the other, so each side stands
alone. See ``tests/fixtures/automation_schema_dump.py`` for the normalization
rules and the cross-repo update rule.

Why the declared schema objects and not a reflected database
------------------------------------------------------------
tests/test_create_automation_tables_migration.py executes this migration against
**SQLite**, and a SQLite reflection cannot be compared to a Postgres-shaped
fixture: ``sa.Enum`` degrades to VARCHAR + CHECK (losing the type name and the
value set), ``timezone=True`` is not represented, JSONB becomes JSON, and server
defaults come back quoted differently. Running this assertion against a real
Postgres instead would mean adding a Postgres container to a suite that has none
(this repo's test infra is MySQL, Elasticsearch and Keycloak) and whose
``docker_services`` fixture yields nothing under GITHUB_ACTIONS — a guard that
only runs on some machines is not a guard.

So this test drives the REAL ``upgrade()`` under the **PostgreSQL dialect in
offline mode** and records the ``sa.Table`` objects it declares. That is what
SQLAlchemy emits the production DDL from, so it carries full Postgres fidelity —
enum type names and members, ``TIMESTAMP WITH TIME ZONE``, JSONB, SMALLINT vs
BIGINT vs INTEGER, and every server default — with no database and no container.

What it consequently does not catch: anything that happens between those objects
and a live Postgres. If SQLAlchemy renders them into DDL correctly (it does — the
same objects, the same compiler), there is no gap; but this test does not prove
the DDL executes, nor that the enum types are created in a workable order.
Execution is covered by tests/test_create_automation_tables_migration.py, which
runs ``upgrade()`` and ``downgrade()`` end to end.
"""

import io
import json
import os

import alembic.op as alembic_op
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.fixtures.automation_schema_dump import AUTOMATION_TABLES, dump_from_tables
from tests.test_create_automation_tables_migration import load_automation_migration

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "automation_schema.json")

with open(GOLDEN_PATH, encoding="utf-8") as _fh:
    GOLDEN = json.load(_fh)["tables"]

SECTIONS = ("columns", "primary_key", "indexes", "unique_constraints", "foreign_keys")


def _record_declared_schema(migration):
    """Run ``upgrade()`` for its declarations, not its side effects.

    ``op.create_table`` returns the ``sa.Table`` it builds, so wrapping it
    captures the full declared schema. ``op.create_index`` builds its ``Index``
    against a throwaway table instead of the real one, so the call's own
    arguments are what gets recorded.

    Offline mode (``as_sql``) means no connection is opened; the emitted DDL goes
    to a buffer we discard. The dialect is postgresql so types resolve the way
    production does — notably ``sa.JSON().with_variant(JSONB, "postgresql")``.
    """
    tables: dict = {}
    indexes: list = []

    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": io.StringIO()},
    )
    with Operations.context(context):
        real_create_table = alembic_op.create_table
        real_create_index = alembic_op.create_index

        def create_table(table_name, *columns, **kw):
            table = real_create_table(table_name, *columns, **kw)
            tables[table_name] = table
            return table

        def create_index(index_name, table_name, columns, **kw):
            indexes.append(
                (index_name, table_name, list(columns), bool(kw.get("unique", False)))
            )
            return real_create_index(index_name, table_name, columns, **kw)

        alembic_op.create_table = create_table
        alembic_op.create_index = create_index
        try:
            migration.upgrade()
        finally:
            alembic_op.create_table = real_create_table
            alembic_op.create_index = real_create_index

    return tables, indexes


@pytest.fixture(scope="module")
def declared():
    return _record_declared_schema(load_automation_migration("automation_schema_probe"))


@pytest.fixture(scope="module")
def migration_dump(declared):
    tables, indexes = declared
    return dump_from_tables(tables, indexes)["tables"]


def test_migration_creates_exactly_the_fixtured_tables(declared):
    """A fourth automation table would otherwise be created but never checked."""
    tables, _ = declared
    assert sorted(tables) == sorted(AUTOMATION_TABLES)


def test_dumped_tables_match_the_fixture(migration_dump):
    assert sorted(migration_dump) == sorted(GOLDEN)


@pytest.mark.parametrize("table", AUTOMATION_TABLES)
@pytest.mark.parametrize("section", SECTIONS)
def test_migration_matches_golden_fixture(migration_dump, table, section):
    """Drift between this migration and keep-automation-api's ORM models.

    A failure here means production DDL and the ORM disagree. Fix whichever side
    is wrong, then regenerate the fixture and update BOTH vendored copies in the
    same change set.
    """
    assert migration_dump[table][section] == GOLDEN[table][section]
