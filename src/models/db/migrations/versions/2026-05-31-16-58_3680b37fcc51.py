"""merge phase1 lastalert enrichment and alert source_to_json branches

Revision ID: 3680b37fcc51
Revises: c4d5e6f7a8b9, source_to_json
Create Date: 2026-05-31 16:58:47.509930

"""

import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "3680b37fcc51"
down_revision = ("c4d5e6f7a8b9", "source_to_json")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
