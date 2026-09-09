"""incident: add dismiss columns, drop the 'deleted' status, make delete a real delete

Three coupled changes behind the new incident suppression behaviour:

1. `incident.dismiss_mode` / `incident.dismissed_until` — the typed dismiss
   state, mirroring what `lastalert` already carries for alerts. "suppressed"
   is NOT written to `incident.status`; it is derived from these two columns at
   read time (`Incident.get_effective_status`) so a time-boxed dismissal can
   expire on its own clock without a sweeper job rewriting rows.

2. `IncidentStatus.DELETED` is gone, so rows still parked in `status='deleted'`
   are unreachable — nothing can query them by status and nothing will ever
   move them out of it. They are deleted for real here.

3. `incidentenrichment_incident_id_fkey` was NO ACTION, which made a hard
   DELETE of any enriched incident fail with a foreign key violation. Recreated
   ON DELETE CASCADE, matching the CASCADE that `lastalerttoincident` and
   `alerttoincident` already declare. Without this, both the backfill in (2)
   and `delete_incident_by_id` raise on exactly the incidents users care about.

IRREVERSIBLE IN PART: `downgrade()` drops the two columns and restores the
FK's NO ACTION rule, but cannot bring back the rows deleted in (2). Take a
backup if those rows still matter.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "incident_dismiss_columns"
down_revision = "create_automation_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident",
        sa.Column("dismiss_mode", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "incident",
        sa.Column("dismissed_until", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial-ish index: the read-time derivation filters on dismissed_until for
    # every incident list query, and the column is NULL for all but dismissed rows.
    op.create_index(
        "ix_incident_dismissed_until",
        "incident",
        ["dismissed_until"],
    )

    # Enrichment rows must follow their incident to the grave now that delete is
    # a real DELETE. Drop-and-recreate is the only way to change a FK's action.
    op.drop_constraint(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        "incident",
        ["incident_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Soft-deleted incidents are unreachable now that the status is gone.
    # lastalerttoincident / alerttoincident / incidentenrichment all cascade;
    # self-referential merge and same-incident links are ON DELETE SET NULL.
    op.execute(sa.text("DELETE FROM incident WHERE status = 'deleted'"))


def downgrade() -> None:
    op.drop_constraint(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        "incident",
        ["incident_id"],
        ["id"],
    )
    op.drop_index("ix_incident_dismissed_until", table_name="incident")
    op.drop_column("incident", "dismissed_until")
    op.drop_column("incident", "dismiss_mode")
