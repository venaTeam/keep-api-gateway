"""Migration coverage for the incident-enrichment table cutover.

Two revisions implement the cutover:
  - ``create_incident_enrichment``: creates ``incidentenrichment`` and backfills
    it from ``alertenrichment`` rows whose ``alert_fingerprint`` matches an
    existing ``incident.id`` (as text) for the same tenant.
  - ``drop_alertenrichment``: drops the legacy ``alertenrichment`` table
    (downgrade recreates an EMPTY table — data is not restored).

We run each migration module's own ``upgrade()`` / ``downgrade()`` against a
connection we own (alembic ``MigrationContext`` + ``Operations``), so the REAL
migration code executes while the test stays hermetic and dialect-portable
(SQLite). This mirrors tests/test_alert_column_drop_migration.py.
"""

import importlib.util
import os
import uuid

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
_CREATE_PATH = os.path.join(
    _MIGRATIONS_DIR, "2026-06-07-10-00_create_incident_enrichment.py"
)
_DROP_PATH = os.path.join(
    _MIGRATIONS_DIR, "2026-06-07-10-01_drop_alertenrichment.py"
)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_pre_cutover_schema(engine):
    """Build the schema the create-revision expects: ``incident`` and
    ``alertenrichment`` (plus a minimal ``tenant`` for the FK).

    ``incident.id`` is modelled as a String holding the canonical dashed UUID
    text. On Postgres ``CAST(uuid AS TEXT)`` yields exactly that dashed form, so
    this faithfully reproduces the production JOIN
    (``CAST(incident.id AS TEXT) = alertenrichment.alert_fingerprint``). SQLite's
    native ``Uuid`` type would instead store/cast as 32 hex chars without
    dashes, which would not represent the real comparison.
    """
    md = sa.MetaData()
    sa.Table(
        "tenant",
        md,
        sa.Column("id", sa.String(), primary_key=True),
    )
    sa.Table(
        "incident",
        md,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String()),
    )
    sa.Table(
        "alertenrichment",
        md,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String()),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("alert_fingerprint", sa.String(), unique=True),
        sa.Column("enrichments", sa.JSON()),
    )
    md.create_all(engine)
    return md


def _tables(engine):
    return set(sa.inspect(engine).get_table_names())


def test_revision_chain_is_linear():
    create = _load(_CREATE_PATH, "create_incident_enrichment_migration")
    drop = _load(_DROP_PATH, "drop_alertenrichment_migration")
    assert create.revision == "create_incident_enrichment"
    assert create.down_revision == "alert_received_at"
    assert drop.revision == "drop_alertenrichment"
    assert drop.down_revision == "create_incident_enrichment"


def test_create_revision_backfills_only_matching_incident_rows(tmp_path):
    create = _load(_CREATE_PATH, "create_incident_enrichment_migration")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        md = _seed_pre_cutover_schema(engine)

        tenant = md.tables["tenant"]
        incident = md.tables["incident"]
        alertenrichment = md.tables["alertenrichment"]

        incident_id = str(uuid.uuid4())
        other_tenant_incident_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(tenant.insert(), [{"id": "t1"}, {"id": "t2"}])
            conn.execute(
                incident.insert(),
                [
                    {"id": incident_id, "tenant_id": "t1"},
                    {"id": other_tenant_incident_id, "tenant_id": "t2"},
                ],
            )
            conn.execute(
                alertenrichment.insert(),
                [
                    # matches incident_id within t1 -> backfilled
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": "t1",
                        "timestamp": None,
                        "alert_fingerprint": str(incident_id),
                        "enrichments": {"status": "acknowledged"},
                    },
                    # an alert fingerprint that is NOT an incident id -> skipped
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": "t1",
                        "timestamp": None,
                        "alert_fingerprint": "plain-alert-fp",
                        "enrichments": {"foo": "bar"},
                    },
                    # right fingerprint but WRONG tenant -> skipped (tenant mismatch)
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": "t1",
                        "timestamp": None,
                        "alert_fingerprint": str(other_tenant_incident_id),
                        "enrichments": {"foo": "baz"},
                    },
                ],
            )

        # --- run the REAL create-revision upgrade against our connection ---
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                create.upgrade()

        assert "incidentenrichment" in _tables(engine)

        with engine.connect() as conn:
            ie = sa.Table(
                "incidentenrichment", sa.MetaData(), autoload_with=conn
            )
            rows = conn.execute(sa.select(ie)).mappings().all()

        # exactly one row backfilled — the tenant+id match
        assert len(rows) == 1
        backfilled = rows[0]
        # compare canonically (SQLite reflection drops the Uuid type, so the
        # stored value reads back as 32 hex chars without dashes)
        assert uuid.UUID(str(backfilled["incident_id"])) == uuid.UUID(incident_id)
        assert backfilled["tenant_id"] == "t1"
        assert backfilled["enrichments"] == {"status": "acknowledged"}

        # --- downgrade drops the table ---
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                create.downgrade()
        assert "incidentenrichment" not in _tables(engine)
    finally:
        engine.dispose()


def test_drop_revision_drops_and_recreates_empty(tmp_path):
    drop = _load(_DROP_PATH, "drop_alertenrichment_migration")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'd.db'}")
    try:
        md = sa.MetaData()
        sa.Table(
            "tenant", md, sa.Column("id", sa.String(), primary_key=True)
        )
        sa.Table(
            "alertenrichment",
            md,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("tenant_id", sa.String()),
            sa.Column("timestamp", sa.DateTime()),
            sa.Column("alert_fingerprint", sa.String(), unique=True),
            sa.Column("enrichments", sa.JSON()),
        )
        md.create_all(engine)
        assert "alertenrichment" in _tables(engine)

        # --- upgrade drops it ---
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                drop.upgrade()
        assert "alertenrichment" not in _tables(engine)

        # --- downgrade recreates the empty shape ---
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                drop.downgrade()
        assert "alertenrichment" in _tables(engine)
        cols = {c["name"] for c in sa.inspect(engine).get_columns("alertenrichment")}
        assert cols == {
            "id",
            "tenant_id",
            "timestamp",
            "alert_fingerprint",
            "enrichments",
        }
        # recreated empty — no data restored
        with engine.connect() as conn:
            ae = sa.Table("alertenrichment", sa.MetaData(), autoload_with=conn)
            assert conn.execute(sa.select(sa.func.count()).select_from(ae)).scalar() == 0
    finally:
        engine.dispose()
