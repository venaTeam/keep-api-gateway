"""merge the automation control-plane head with the multi-tenant head

Both lineages descend from `drop_commentmention_audit_fk` and touch DISJOINT
schema, so this is a no-op merge that simply unifies the two alembic heads:

  - `create_automation_tables` — the automation control plane: `automations`,
                                 `automation_runs`, `automation_revisions`.
  - `preset_tag_name_per_tenant` — multi-tenant: `operator` and `tenantrolegrant`,
                                 `tenant.uq_tenant_name`, and per-tenant uniques
                                 on `preset` / `tag`.

Revision ID: merge_automation_multitenant
Revises: create_automation_tables, preset_tag_name_per_tenant
Create Date: 2026-08-27
"""

import sqlalchemy as sa  # noqa: F401
from alembic import op  # noqa: F401

# revision identifiers, used by Alembic.
revision = "merge_automation_multitenant"
down_revision = ("create_automation_tables", "preset_tag_name_per_tenant")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: the two merged lineages touch disjoint tables."""
    pass


def downgrade() -> None:
    """No-op: splitting back into two heads requires no schema change."""
    pass
