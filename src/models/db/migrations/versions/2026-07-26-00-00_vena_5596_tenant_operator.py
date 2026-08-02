"""VENA-5596: tenant/operator management schema

Adds the schema the tenant + operator management API writes to, in one atomic
upgrade:

  * operator          -- Keep-owned routing keys. `name`, `group`, and `apikey`
                         are each globally unique; `group` global uniqueness is
                         the "one operator per group across all tenants" rule.
                         `tenant_id` links an operator to its owning tenant.
  * tenantrolegrant   -- per-tenant role grants keyed by the identifier the token
                         carries (email for a user, group path for a group).
                         (tenant_id, subject) is the primary key.
  * tenant.name       -- promoted to UNIQUE so duplicate names are rejected at the
                         DB level (race-safe), replacing the old check-then-insert.

Revision ID: vena_5596_tenant_operator
Revises: preset_filter_indexes
Create Date: 2026-07-26
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "vena_5596_tenant_operator"
down_revision = "preset_filter_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("group", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("apikey", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_operator_name"),
        sa.UniqueConstraint("group", name="uq_operator_group"),
        sa.UniqueConstraint("apikey", name="uq_operator_apikey"),
    )
    op.create_index("ix_operator_name", "operator", ["name"])
    op.create_index("ix_operator_group", "operator", ["group"])

    op.create_table(
        "tenantrolegrant",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "subject"),
    )

    # Promote tenant.name to UNIQUE. Abort loudly if duplicates already exist so
    # the failure names the offenders instead of a cryptic constraint error.
    bind = op.get_bind()
    dupes = bind.execute(
        sa.text("SELECT name FROM tenant GROUP BY name HAVING COUNT(*) > 1")
    ).fetchall()
    if dupes:
        raise RuntimeError(
            "Cannot add unique constraint on tenant.name -- duplicate names exist: "
            + ", ".join(sorted(str(d[0]) for d in dupes))
        )
    with op.batch_alter_table("tenant") as batch_op:
        batch_op.create_unique_constraint("uq_tenant_name", ["name"])


def downgrade() -> None:
    with op.batch_alter_table("tenant") as batch_op:
        batch_op.drop_constraint("uq_tenant_name", type_="unique")
    op.drop_table("tenantrolegrant")
    op.drop_index("ix_operator_group", table_name="operator")
    op.drop_index("ix_operator_name", table_name="operator")
    op.drop_table("operator")
