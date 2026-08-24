"""merge_sc05_and_tenant_operator

Revision ID: d189e52482f1
Revises: drop_commentmention_audit_fk, vena_5596_tenant_operator
Create Date: 2026-08-09 16:51:14.569120

"""
import sqlalchemy as sa
import sqlalchemy_utils
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd189e52482f1'
down_revision = ('drop_commentmention_audit_fk', 'vena_5596_tenant_operator')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
