"""drop the legacy alertenrichment table

The alert enrichment path now lives entirely on typed ``lastalert`` columns and
incident enrichment has moved to the dedicated ``incidentenrichment`` table
(``create_incident_enrichment``), so the dual-use ``alertenrichment`` table is no
longer read or written. This drops it.

ONE-WAY: ``downgrade`` recreates an EMPTY ``alertenrichment`` table matching the
old shape so the schema is structurally reversible, but the dropped rows are NOT
restored. Take a backup before applying in production if the legacy rows are
still needed.

Revision ID: drop_alertenrichment
Revises: create_incident_enrichment
Create Date: 2026-06-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "drop_alertenrichment"
down_revision = "create_incident_enrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("alertenrichment")


def downgrade() -> None:
    # Recreate the empty legacy shape only. Data is intentionally NOT restored.
    op.create_table(
        "alertenrichment",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("alert_fingerprint", sa.String(), nullable=False, unique=True),
        sa.Column(
            "enrichments",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
