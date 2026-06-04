"""Misplaced-alert-column drop migration coverage for e2b3c4d5f6a7.

`e2b3c4d5f6a7` is the ONLY irreversible-in-place step of the alertenrichment
removal: it DROPs 11
columns from the append-only ``alert`` table (system tracking + user state that
moved to ``lastalert``; plus the denormalized ``incident``). A regression here
silently deletes a production column, so the upgrade/downgrade is exercised
end-to-end against a real (SQLite) engine.

Rather than booting alembic's env.py (which builds its own engine from a
module-level config constant frozen at import time — not test-controllable), we
run the migration module's own ``upgrade()`` / ``downgrade()`` against a
connection we own, using alembic's ``MigrationContext`` + ``Operations`` context.
This executes the REAL migration code (the same ``op.drop_column`` /
``op.add_column`` calls) while keeping the test hermetic and dialect-portable.
"""

import importlib.util
import os

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src",
    "models",
    "db",
    "migrations",
    "versions",
)
_MIGRATION_PATH = os.path.join(_MIGRATIONS_DIR, "2026-05-27-10-30_e2b3c4d5f6a7.py")

# The 11 columns the migration drops from ``alert``.
_EXPECTED_DROPPED = {
    "last_received",
    "firing_counter",
    "unresolved_counter",
    "started_at",
    "firing_start_time",
    "firing_start_time_since_last_resolved",
    "assignee",
    "dismissed",
    "dismiss_until",
    "note",
    "incident",
}
# Immutable provider columns that must survive the drop.
_CORE_KEPT = {"status", "severity", "description"}


def _load_migration():
    spec = importlib.util.spec_from_file_location("alert_column_drop_migration", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_pre_drop_alert_table(engine):
    """Create an ``alert`` table in the state left by d1a2b3c4e5f6: it carries
    all 11 to-be-dropped columns plus the core provider columns."""
    md = sa.MetaData()
    sa.Table(
        "alert",
        md,
        sa.Column("id", sa.String(255), primary_key=True),
        # core provider columns (must remain after upgrade)
        sa.Column("status", sa.String(255)),
        sa.Column("severity", sa.String(255)),
        sa.Column("description", sa.Text()),
        # the 11 columns the migration drops
        sa.Column("last_received", sa.DateTime(timezone=True)),
        sa.Column("firing_counter", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unresolved_counter", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.String(255)),
        sa.Column("firing_start_time", sa.String(255)),
        sa.Column("firing_start_time_since_last_resolved", sa.String(255)),
        sa.Column("assignee", sa.String(255)),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dismiss_until", sa.String(255)),
        sa.Column("note", sa.Text()),
        sa.Column("incident", sa.String(255)),
    )
    md.create_all(engine)


def _alert_columns(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("alert")}


def test_migration_constants_are_correct():
    """Guard the revision chain + the drop list before exercising the DDL."""
    migration = _load_migration()
    assert migration.revision == "e2b3c4d5f6a7"
    assert migration.down_revision == "d1a2b3c4e5f6"  # linear chain
    dropped_names = {name for name, *_ in migration._DROPPED_COLUMNS}
    assert dropped_names == _EXPECTED_DROPPED
    assert len(migration._DROPPED_COLUMNS) == 11


def test_upgrade_drops_columns_downgrade_readds(tmp_path):
    migration = _load_migration()
    db_path = tmp_path / "migration.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        _build_pre_drop_alert_table(engine)

        # sanity: pre-state has both the doomed and the core columns
        before = _alert_columns(engine)
        assert _EXPECTED_DROPPED <= before
        assert _CORE_KEPT <= before

        # --- upgrade: run the REAL migration upgrade() against our connection ---
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

        after_upgrade = _alert_columns(engine)
        # all 11 dropped...
        assert not (_EXPECTED_DROPPED & after_upgrade), (
            f"columns survived upgrade: {_EXPECTED_DROPPED & after_upgrade}"
        )
        # ...core provider columns remain
        assert _CORE_KEPT <= after_upgrade

        # --- downgrade: columns re-added ---
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.downgrade()

        after_downgrade = _alert_columns(engine)
        assert _EXPECTED_DROPPED <= after_downgrade, (
            f"columns not re-added on downgrade: "
            f"{_EXPECTED_DROPPED - after_downgrade}"
        )
        assert _CORE_KEPT <= after_downgrade
    finally:
        engine.dispose()
