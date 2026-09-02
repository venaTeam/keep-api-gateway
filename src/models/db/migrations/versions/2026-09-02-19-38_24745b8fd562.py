"""merge heads

Revision ID: 24745b8fd562
Revises: preset_filter_indexes, alertraw_partition_dlq
Create Date: 2026-09-02 19:38:44.844328

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "24745b8fd562"
down_revision = ("preset_filter_indexes", "alertraw_partition_dlq")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
