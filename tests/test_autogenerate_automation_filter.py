"""Regression guard for the alembic autogenerate data-loss footgun.

`automations`, `automation_runs` and `automation_revisions` live in the `keep`
database and are migrated by THIS repo's alembic lineage, but their ORM models
stay in keep-automation-api — so they will never appear in gateway's
``target_metadata = SQLModel.metadata``.

To autogenerate, a table in the database but not in ``target_metadata`` reads as
a table to delete. Without ``include_object`` the next
``alembic revision --autogenerate`` emits ``op.drop_table("automations")`` and
destroys the automation control plane. These assertions are the guard on that.

``env.py`` cannot be imported outside an alembic run (it executes the migrations
at import time), which is exactly why the filter lives in its own module — so it
can be tested directly, as the real hook object, here.

WHAT MAKES THIS GUARD REAL
--------------------------
The subjects under test are pinned to the MIGRATION, never to a literal in this
file. Feeding ``include_object`` the same constant it is meant to police proves
nothing: the loop and the filter would drift together, and an enum quietly
deleted from ``AUTOMATION_ENUM_TYPES`` would still show 7 passed while becoming
autogenerate-visible again. So ``migrated_tables`` is observed by executing the
revision's ``upgrade()``, and ``migrated_enum_names`` is read off the revision's
own ``ENUM_TYPES``. Drop an entry from either constant and these tests fail.
"""

import pytest
import sqlalchemy as sa

from src.models.db.migrations.autogenerate_filters import (
    AUTOMATION_ENUM_TYPES,
    AUTOMATION_TABLES,
    include_object,
)
from tests.test_create_automation_tables_migration import (
    created_table_names,
    load_automation_migration,
)

# Gateway-owned tables. These are modelled in this repo, so autogenerate must
# keep reasoning about them — a filter that hid them would silently freeze the
# platform schema. Literal on purpose: they are not what the migration creates.
GATEWAY_TABLES = ("alert", "tenant", "incident", "preset", "lastalert")


@pytest.fixture(scope="module")
def migrated_tables():
    """The tables the automation revision really creates, by running it."""
    return created_table_names()


@pytest.fixture(scope="module")
def migrated_enum_names():
    """The native PG enum types the automation revision really creates."""
    migration = load_automation_migration("automation_migration_for_enum_pin")
    return {enum_type.name for enum_type in migration.ENUM_TYPES}


def _table(name):
    """A real reflected-shape Table object, as alembic hands to the hook."""
    return sa.Table(name, sa.MetaData(), sa.Column("id", sa.String()))


def test_automation_tables_are_excluded_from_autogenerate(migrated_tables):
    for name in sorted(migrated_tables):
        assert (
            include_object(_table(name), name, "table", True, None) is False
        ), f"{name} is visible to autogenerate — a --autogenerate run would DROP it"


def test_gateway_tables_are_still_included():
    for name in GATEWAY_TABLES:
        assert (
            include_object(_table(name), name, "table", True, None) is True
        ), f"{name} was hidden from autogenerate — gateway schema changes would vanish"


def test_columns_of_automation_tables_are_excluded(migrated_tables):
    """A reflected column carries its own type_; the table check alone misses it."""
    for name in sorted(migrated_tables):
        table = sa.Table(name, sa.MetaData(), sa.Column("tenant_id", sa.String()))
        column = table.c.tenant_id
        assert (
            include_object(column, "tenant_id", "column", True, None) is False
        ), f"{name}.tenant_id is visible to autogenerate"


def test_columns_of_gateway_tables_are_still_included():
    table = sa.Table("alert", sa.MetaData(), sa.Column("tenant_id", sa.String()))
    assert include_object(table.c.tenant_id, "tenant_id", "column", True, None) is True


def test_automation_enum_types_are_excluded(migrated_enum_names):
    for name in sorted(migrated_enum_names):
        assert (
            include_object(sa.Enum(name=name), name, "type", True, None) is False
        ), f"enum type {name} is visible to autogenerate"


def test_unrelated_enum_type_is_still_included():
    assert (
        include_object(sa.Enum(name="aisuggestiontype"), "aisuggestiontype", "type", True, None)
        is True
    )


def test_excluded_set_matches_the_migrated_tables(migrated_tables):
    """The constant and the migration must not drift apart."""
    assert AUTOMATION_TABLES == migrated_tables


def test_excluded_enum_set_matches_the_migrated_enum_types(migrated_enum_names):
    """Same pin for the enum types: an enum the revision creates but the filter
    does not list is an object autogenerate would try to DROP."""
    assert AUTOMATION_ENUM_TYPES == migrated_enum_names
