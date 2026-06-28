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

NOT indexed here (intentional):
  - status: compiles to COALESCE(lastalert.status, alert.status) — a cross-table
    expression that is not sargable by any single-column index.
  - application.contains(...): compiles to LIKE '%x%' — needs a pg_trgm GIN index,
    deferred until those presets are shown to be hot.
  - object: equality/IN predicates exist (~4 presets) but are deferred for now.

Built with CREATE INDEX CONCURRENTLY (Postgres) so the build does not take an
ACCESS EXCLUSIVE lock on the live alert/lastalert tables.  CONCURRENTLY cannot run
inside a transaction, so the operations run in an autocommit block.

Revision ID: preset_filter_indexes
Revises: alert_environment
Create Date: 2026-06-25
"""

from alembic import op

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


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        # CONCURRENTLY requires running outside a transaction.
        with op.get_context().autocommit_block():
            for name, table, cols in _INDEXES:
                op.create_index(
                    name,
                    table,
                    cols,
                    if_not_exists=True,
                    postgresql_concurrently=True,
                )
    else:
        # SQLite/MySQL test paths: plain create (no CONCURRENTLY support).
        for name, table, cols in _INDEXES:
            op.create_index(name, table, cols, if_not_exists=True)


def downgrade() -> None:
    if _is_postgres():
        with op.get_context().autocommit_block():
            for name, table, _cols in reversed(_INDEXES):
                op.drop_index(
                    name,
                    table_name=table,
                    if_exists=True,
                    postgresql_concurrently=True,
                )
    else:
        for name, table, _cols in reversed(_INDEXES):
            op.drop_index(name, table_name=table, if_exists=True)
