"""Add lastalert.deleted (alertenrichment removal — the lastalert.deleted migration)

Revision ID: d1a2b3c4e5f6
Revises: c4d5e6f7a8b9
Create Date: 2026-05-27 10:00:00.000000

The lastalert.deleted step of the alertenrichment removal (PHASE2_IMPLEMENTATION_PLAN.md).

Adds a typed ``deleted`` boolean to ``lastalert`` to replace the legacy
``alertenrichment.enrichments->'deletedAt'`` timestamp-list mechanism (which drove
``AlertDto.deleted`` via ``parse_and_enrich_deleted_and_assignees``). Under the strict
schema there is no JSONB to hold ``deletedAt``; ``deleted`` becomes a first-class column
set by the ``delete_alert`` route and cleared by ``unenrich``/restore.

NOT-NULL with ``server_default = false`` so the OLD code path (which inserts a LastAlert
without this column) keeps working during the cutover window (NF6).

Backfill note: the legacy ``deletedAt`` array stores client-sent ISO strings whose format
does not reliably match ``lastalert.last_received`` (timestamptz). Matching in SQL is
fragile and the value is niche, so the backfill is intentionally SKIPPED — accepted
data-loss, consistent with the spec's treatment of dynamic enrichments. Existing
currently-deleted alerts therefore default to ``deleted = FALSE``.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d1a2b3c4e5f6"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lastalert",
        sa.Column(
            "deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("lastalert", "deleted")
