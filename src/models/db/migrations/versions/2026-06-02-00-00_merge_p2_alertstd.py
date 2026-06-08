"""merge the alertenrichment-removal head with the alert-standardization / source_to_json head

Both lineages descend from `c4d5e6f7a8b9` (the Phase 1 lastalert head) and touch DISJOINT
`alert` columns, so this is a no-op merge that simply unifies the two alembic heads:

  - `f3a4b5c6d7e8` — alertenrichment removal: typed `lastalert` enrichment+tracking columns,
                     ticket columns, and the drop of the 11 misplaced `alert` columns.
  - `3680b37fcc51` — dev: alert standardization (site/impact/runbook_url/alert_rule_url) +
                     `source` String -> JSON (which itself merged `c4d5e6f7a8b9` + `source_to_json`).

Revision ID: merge_p2_alertstd
Revises: f3a4b5c6d7e8, 3680b37fcc51
Create Date: 2026-06-02
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers, used by Alembic.
revision = "merge_p2_alertstd"
down_revision = ("f3a4b5c6d7e8", "3680b37fcc51")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: the two merged lineages modify disjoint columns."""
    pass


def downgrade() -> None:
    """No-op: splitting back into two heads requires no schema change."""
    pass
