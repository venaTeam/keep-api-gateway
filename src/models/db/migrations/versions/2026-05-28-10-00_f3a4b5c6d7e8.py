"""Add lastalert ticket_type / ticket_url / ticket_provider_id (alertenrichment removal)

Revision ID: f3a4b5c6d7e8
Revises: e2b3c4d5f6a7
Create Date: 2026-05-28 10:00:00.000000

alertenrichment-removal follow-up. Previously the assign-ticket modal wrote arbitrary keys
(`ticket_type`, `ticket_url`, `ticket_provider_id`) into ``alertenrichment.enrichments``.
Under the strict schema those keys now 422. Per product decision they get
first-class typed columns on ``lastalert`` instead of being discarded.

All three are nullable string columns with no server_default — they only apply
when a ticket is actually attached and otherwise stay NULL (so the AlertDto
reverts to the provider value, matching the spec's "NULL = no override"
semantics for every other user-enrichment column).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f3a4b5c6d7e8"
down_revision = "e2b3c4d5f6a7"
branch_labels = None
depends_on = None


_NEW_COLUMNS = [
    ("ticket_type", sa.String(50)),
    ("ticket_url", sa.String(500)),
    ("ticket_provider_id", sa.String(255)),
]


def upgrade() -> None:
    for name, type_ in _NEW_COLUMNS:
        op.add_column("lastalert", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_NEW_COLUMNS):
        op.drop_column("lastalert", name)
