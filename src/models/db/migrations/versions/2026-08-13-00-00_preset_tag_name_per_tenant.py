"""Make preset and tag names unique per tenant, not globally

Preset had both the correct per-tenant UniqueConstraint("tenant_id", "name")
and a stray global UniqueConstraint("name") -- the latter blocked two tenants
from using the same preset name. Tag had only a global unique on name.

This drops the global uniques (preset_name_key, tag_name_key) and gives tag a
per-tenant composite unique. The preset per-tenant composite already exists.

Revision ID: preset_tag_name_per_tenant
Revises: d189e52482f1
Create Date: 2026-08-13
"""

from alembic import op

revision = "preset_tag_name_per_tenant"
down_revision = "d189e52482f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("ALTER TABLE preset DROP CONSTRAINT IF EXISTS preset_name_key")
        op.execute("ALTER TABLE tag DROP CONSTRAINT IF EXISTS tag_name_key")
        op.create_unique_constraint(
            "uq_tag_tenant_name", "tag", ["tenant_id", "name"]
        )
    else:
        # SQLite/others: batch-recreate to drop the global uniques.
        with op.batch_alter_table("preset") as batch_op:
            try:
                batch_op.drop_constraint("preset_name_key", type_="unique")
            except Exception:
                pass
        with op.batch_alter_table("tag") as batch_op:
            try:
                batch_op.drop_constraint("tag_name_key", type_="unique")
            except Exception:
                pass
            batch_op.create_unique_constraint(
                "uq_tag_tenant_name", ["tenant_id", "name"]
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.drop_constraint("uq_tag_tenant_name", "tag", type_="unique")
        op.create_unique_constraint("tag_name_key", "tag", ["name"])
        op.create_unique_constraint("preset_name_key", "preset", ["name"])
    else:
        with op.batch_alter_table("tag") as batch_op:
            batch_op.drop_constraint("uq_tag_tenant_name", type_="unique")
            batch_op.create_unique_constraint("tag_name_key", ["name"])
        with op.batch_alter_table("preset") as batch_op:
            batch_op.create_unique_constraint("preset_name_key", ["name"])
