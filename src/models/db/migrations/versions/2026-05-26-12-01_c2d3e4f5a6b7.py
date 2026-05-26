"""phase1: add lastalert indexes (status, assignee, dismissed_until)

Revision ID: c2d3e4f5a6b7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-26 12:01:00.000000

Phase 1 / task 1.2 (Spec F25-F27).

On PostgreSQL the indexes are built with CREATE INDEX CONCURRENTLY so the live
table is not locked (NF4/NF5). CONCURRENTLY cannot run inside a transaction
block, so it is wrapped in ``op.get_context().autocommit_block()``. On other
dialects a regular (locking) index is created.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c2d3e4f5a6b7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

_INDEXES = [
    ("idx_lastalert_tenant_status", ["tenant_id", "status"]),
    ("idx_lastalert_tenant_assignee", ["tenant_id", "assignee"]),
    ("idx_lastalert_tenant_dismissed_until", ["tenant_id", "dismissed_until"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY must run outside a transaction.
        with op.get_context().autocommit_block():
            for name, cols in _INDEXES:
                op.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                    f"ON lastalert ({', '.join(cols)})"
                )
    else:
        for name, cols in _INDEXES:
            op.create_index(name, "lastalert", cols)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _ in _INDEXES:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    else:
        for name, _ in _INDEXES:
            op.drop_index(name, table_name="lastalert")
