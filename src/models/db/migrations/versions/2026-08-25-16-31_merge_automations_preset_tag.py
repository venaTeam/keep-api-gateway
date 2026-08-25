"""merge automations and preset-tag-per-tenant heads

Revision ID: merge_automations_preset_tag
Revises: create_automation_tables, preset_tag_name_per_tenant
Create Date: 2026-08-25 16:31:16.756798

"""

# revision identifiers, used by Alembic.
revision = "merge_automations_preset_tag"
down_revision = ("create_automation_tables", "preset_tag_name_per_tenant")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
