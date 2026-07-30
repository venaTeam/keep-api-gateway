"""Execution coverage for the automation control-plane table creation.

``create_automation_tables`` builds `automations`, `automation_runs` and
`automation_revisions` in the `keep` database. Nothing else in any repo executes
it: ``tests/conftest.py`` builds the test database with
``SQLModel.metadata.create_all``, and gateway's metadata deliberately EXCLUDES
these three tables (their ORM models stay in keep-automation-api — see
``src/models/db/migrations/autogenerate_filters.py``), so ``create_all`` does not
build them either. Without this file a wrong column type, a bad FK target or a
broken drop ordering ships unverified.

As in tests/test_alert_column_drop_migration.py and
tests/test_incident_enrichment_migration.py, the migration module is loaded by
path and its own ``upgrade()`` / ``downgrade()`` are driven through alembic's
``MigrationContext`` + ``Operations`` against a connection we own. That runs the
REAL migration code while staying hermetic and dialect-portable (SQLite).

SQLite caveats, and what they do and do not cover:
  - the FK target table (`tenant`) is created first, so the FKs resolve;
  - ``sa.Enum`` renders as VARCHAR + CHECK rather than a native PG type, so
    ``DROP TYPE`` is a no-op here. The Postgres-only drop *ordering* (tables
    before types) is therefore not observable; what IS covered is that the
    downgrade removes every table and completes without error. The companion
    guard that no enum can go missing from the drop loop lives in
    tests/test_autogenerate_automation_filter.py, which pins the filter
    constants against this module's ``ENUM_TYPES``.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src",
    "models",
    "db",
    "migrations",
    "versions",
    "2026-07-30-00-00_create_automation_tables.py",
)

# The three tables the revision creates.
AUTOMATION_TABLE_NAMES = ("automations", "automation_runs", "automation_revisions")

# Tables that carry an enforced tenant FK. `automation_revisions` is absent on
# purpose — see test_automation_revisions_has_no_tenant_id.
TENANT_SCOPED_TABLES = ("automations", "automation_runs")

# Every index the revision must create, spelled out independently of the module.
EXPECTED_INDEXES = {
    "automations": {
        "ix_automations_matching_state_tenant_id": ["matching_state", "tenant_id"],
        "ix_automations_build_state_lock": ["build_state", "build_lock_deadline"],
        "ix_automations_tenant_id_namespace": ["tenant_id", "namespace"],
        "ix_automations_tenant_id_created_at": ["tenant_id", "created_at"],
    },
    "automation_runs": {
        "ix_automation_runs_state_created_at": ["state", "created_at"],
        "ix_automation_runs_automation_id_created_at": [
            "automation_id",
            "created_at",
        ],
    },
    "automation_revisions": {
        # Postgres does not auto-index FK columns; without this the
        # revision-history read seq-scans.
        "ix_automation_revisions_automation_id_created_at": [
            "automation_id",
            "created_at",
        ],
    },
}


def load_automation_migration(module_name="create_automation_tables_migration"):
    """Import the revision module by path.

    Public because tests/test_autogenerate_automation_filter.py pins the
    autogenerate filter constants against this same module.
    """
    spec = importlib.util.spec_from_file_location(module_name, MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    """Run one direction of the REAL migration against our own connection."""
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            step()


def _seed_fk_targets(engine):
    """The gateway-owned tables the revision's FKs point at."""
    md = sa.MetaData()
    sa.Table("tenant", md, sa.Column("id", sa.String(), primary_key=True))
    md.create_all(engine)


def created_table_names():
    """The tables ``upgrade()`` actually creates, observed by executing it.

    Exported so tests/test_autogenerate_automation_filter.py can pin the
    autogenerate exclusion list against the migration itself instead of against
    a second literal that drifts silently.
    """
    engine = sa.create_engine("sqlite://")
    try:
        _seed_fk_targets(engine)
        before = set(sa.inspect(engine).get_table_names())
        _run(engine, load_automation_migration().upgrade)
        return set(sa.inspect(engine).get_table_names()) - before
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migration():
    return load_automation_migration()


@pytest.fixture
def engine(tmp_path):
    """An engine with only the FK target (`tenant`) present, as the revision
    expects: `automations.tenant_id` and `automation_runs.tenant_id` reference
    `tenant.id`."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'automations.db'}")
    try:
        _seed_fk_targets(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def migrated_engine(engine, migration):
    _run(engine, migration.upgrade)
    return engine


def _tables(engine):
    return set(sa.inspect(engine).get_table_names())


def _columns(engine, table):
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_revision_chain_is_linear(migration):
    assert migration.revision == "create_automation_tables"
    assert migration.down_revision == "drop_commentmention_audit_fk"


def test_upgrade_creates_all_three_tables(migrated_engine):
    present = _tables(migrated_engine)
    missing = set(AUTOMATION_TABLE_NAMES) - present
    assert not missing, f"upgrade() did not create: {sorted(missing)}"


@pytest.mark.parametrize("table", TENANT_SCOPED_TABLES)
def test_tenant_id_is_not_null_and_fks_to_tenant(migrated_engine, table):
    """A nullable tenant_id, or one that is a plain advisory column, would make
    tenant scoping unenforceable — the whole reason these tables were co-located
    into the `keep` database."""
    tenant_id = _columns(migrated_engine, table).get("tenant_id")
    assert tenant_id is not None, f"{table}.tenant_id is missing"
    assert tenant_id["nullable"] is False, f"{table}.tenant_id is nullable"

    fks = [
        fk
        for fk in sa.inspect(migrated_engine).get_foreign_keys(table)
        if fk["constrained_columns"] == ["tenant_id"]
    ]
    assert len(fks) == 1, f"{table}.tenant_id has no single FK: {fks}"
    assert fks[0]["referred_table"] == "tenant"
    assert fks[0]["referred_columns"] == ["id"]


@pytest.mark.parametrize("table", ("automation_runs", "automation_revisions"))
def test_child_tables_fk_to_automations(migrated_engine, table):
    fks = [
        fk
        for fk in sa.inspect(migrated_engine).get_foreign_keys(table)
        if fk["constrained_columns"] == ["automation_id"]
    ]
    assert len(fks) == 1, f"{table}.automation_id has no single FK: {fks}"
    assert fks[0]["referred_table"] == "automations"
    assert fks[0]["referred_columns"] == ["id"]


def test_automation_revisions_has_no_tenant_id(migrated_engine):
    """Deliberate (§4.4): a revision is only ever reached through its parent
    automation, so `automations.tenant_id` already scopes it. A tenant_id here
    would be a second copy of the truth to keep correct for no query it enables.
    """
    assert "tenant_id" not in _columns(migrated_engine, "automation_revisions")


@pytest.mark.parametrize("table", AUTOMATION_TABLE_NAMES)
def test_indexes_match_expectations(migrated_engine, table):
    """Exact equality, so both a dropped index and a stray one are caught."""
    reflected = {
        idx["name"]: list(idx["column_names"])
        for idx in sa.inspect(migrated_engine).get_indexes(table)
    }
    assert reflected == EXPECTED_INDEXES[table]


def test_run_idempotency_unique_constraint(migrated_engine):
    """`(history_id, automation_id)` IS the submit idempotency authority — the
    Redis gates in keep-automation-consumer are an optimization, not the source
    of truth. Losing this constraint means duplicate automation runs."""
    uniques = {
        uq["name"]: list(uq["column_names"])
        for uq in sa.inspect(migrated_engine).get_unique_constraints("automation_runs")
    }
    assert uniques == {
        "uq_automation_runs_history_automation": ["history_id", "automation_id"]
    }


def test_downgrade_drops_all_three_tables(migrated_engine, migration):
    _run(migrated_engine, migration.downgrade)
    remaining = _tables(migrated_engine) & set(AUTOMATION_TABLE_NAMES)
    assert not remaining, f"downgrade() left tables behind: {sorted(remaining)}"
    # the FK target is gateway-owned and must survive
    assert "tenant" in _tables(migrated_engine)
