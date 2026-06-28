"""add indexes for preset CEL filter predicates on alert/lastalert

Production presets filter alerts almost entirely on a handful of equality
predicates that all land on the `alert` table once the CEL query is compiled to
SQL:

    operator      -> alert.operator          (in ~33 of 45 production presets)
    application   -> alert.application        (~22 presets; operator+application ~18)
    source        -> alert.provider_type      (~5 presets; `source` maps to provider_type)

None of these columns were indexed, so a selective preset had to be resolved by
filtering rows post-fetch.  The preset query drives from `lastalert`
(tenant_id + timestamp threshold + ORDER BY timestamp) and joins `alert` by its
primary key, which means the planner can only use these new alert-side indexes if
it is also able to join *back* from a filtered alert set into lastalert — and
`lastalert.alert_id` had no index.  The (tenant_id, alert_id) index closes that
gap so the planner can start from the selective alert filter when it is cheaper.

All indexes are tenant_id-leading: every preset query filters tenant_id, and
leading with it keeps each tenant's rows clustered in the index.

Partitioning
------------
`alert` is a declaratively partitioned table in production.  Postgres forbids
CREATE/DROP INDEX CONCURRENTLY on a partitioned parent, and a plain CREATE INDEX
on the parent takes a SHARE lock that blocks ingestion on every partition for the
whole build.  So for a partitioned table we use the online pattern:

    1. CREATE INDEX ON ONLY <parent>        -- metadata-only template; also makes
                                               future partitions inherit the index
    2. CREATE INDEX CONCURRENTLY on each existing partition
    3. ALTER INDEX <parent> ATTACH PARTITION <child>  -- parent validates when all
                                                          children are attached

Non-partitioned tables (e.g. lastalert) use a straight CONCURRENTLY build.
Detection is at runtime (pg_class.relkind = 'p'), so the migration is correct
regardless of which tables are partitioned, and every step is idempotent on
rerun.

NOT indexed here (intentional):
  - status: compiles to COALESCE(lastalert.status, alert.status) — a cross-table
    expression that is not sargable by any single-column index.
  - application.contains(...): compiles to LIKE '%x%' — needs a pg_trgm GIN index,
    deferred until those presets are shown to be hot.
  - object: equality/IN predicates exist (~4 presets) but are deferred for now.

Revision ID: preset_filter_indexes
Revises: alert_environment
Create Date: 2026-06-25
"""

import hashlib

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "preset_filter_indexes"
down_revision = "alert_environment"
branch_labels = None
depends_on = None


# (index name, table, columns)
_INDEXES = [
    ("idx_alert_tenant_operator_application", "alert", ["tenant_id", "operator", "application"]),
    ("idx_alert_tenant_application", "alert", ["tenant_id", "application"]),
    ("idx_alert_tenant_provider_type", "alert", ["tenant_id", "provider_type"]),
    ("idx_lastalert_tenant_alert_id", "lastalert", ["tenant_id", "alert_id"]),
]


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _is_partitioned(bind, table: str) -> bool:
    return bool(
        bind.execute(
            text("SELECT relkind = 'p' FROM pg_class WHERE relname = :t"),
            {"t": table},
        ).scalar()
    )


def _partitions(bind, table: str) -> list[str]:
    # Direct child partitions only (single-level partitioning).
    return list(
        bind.execute(
            text(
                """
                SELECT c.relname
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = :t
                ORDER BY c.relname
                """
            ),
            {"t": table},
        )
        .scalars()
        .all()
    )


def _index_is_valid(bind, name: str) -> bool:
    return bool(
        bind.execute(
            text(
                """
                SELECT ix.indisvalid
                FROM pg_class c
                JOIN pg_index ix ON ix.indexrelid = c.oid
                WHERE c.relname = :n
                """
            ),
            {"n": name},
        ).scalar()
    )


def _is_attached(bind, parent_idx: str, child_idx: str) -> bool:
    return bool(
        bind.execute(
            text(
                """
                SELECT 1
                FROM pg_inherits i
                JOIN pg_class p ON p.oid = i.inhparent
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE p.relname = :p AND c.relname = :c
                """
            ),
            {"p": parent_idx, "c": child_idx},
        ).scalar()
    )


def _child_index_name(parent_name: str, partition: str) -> str:
    # Keep <= 63 bytes and unique per (parent index, partition).
    digest = hashlib.md5(f"{parent_name}:{partition}".encode()).hexdigest()[:8]
    return f"{parent_name[:50]}_{digest}"


def _cols_sql(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


def _ensure_index(bind, name, table, cols) -> None:
    """Build index `name` on `table` online and idempotently.

    Leaf table  -> CREATE INDEX CONCURRENTLY.
    Partitioned -> CREATE INDEX ON ONLY <table>, then recurse into each child
    (covering arbitrary sub-partition depth) and ATTACH it.  Recursion is what
    makes a CONCURRENTLY build land only on real leaf partitions; an intermediate
    partitioned level forbids CONCURRENTLY and gets the ON ONLY + ATTACH treatment.
    """
    cols_sql = _cols_sql(cols)
    if not _is_partitioned(bind, table):
        op.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
            f'ON "{table}" ({cols_sql})'
        )
        return

    if _index_is_valid(bind, name):
        return  # already built and fully attached on a previous run

    op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON ONLY "{table}" ({cols_sql})')
    for partition in _partitions(bind, table):
        child = _child_index_name(name, partition)
        _ensure_index(bind, child, partition, cols)
        if not _is_attached(bind, name, child):
            op.execute(f'ALTER INDEX "{name}" ATTACH PARTITION "{child}"')


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        # SQLite/MySQL test paths: plain create (no CONCURRENTLY / partitioning).
        for name, table, cols in _INDEXES:
            op.create_index(name, table, cols, if_not_exists=True)
        return

    # CONCURRENTLY (and the per-partition build) require running outside a txn.
    with op.get_context().autocommit_block():
        for name, table, cols in _INDEXES:
            _ensure_index(bind, name, table, cols)


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        for name, table, _cols in reversed(_INDEXES):
            op.drop_index(name, table_name=table, if_exists=True)
        return

    with op.get_context().autocommit_block():
        for name, table, _cols in reversed(_INDEXES):
            if _is_partitioned(bind, table):
                # Dropping the partitioned parent index cascades to its children.
                # CONCURRENTLY is not allowed on a partitioned index.
                op.execute(f'DROP INDEX IF EXISTS "{name}"')
            else:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
