"""SC-05 step 3 (prep): drop the commentmention -> alertaudit FK

To let the DBA range-partition `alertaudit` by `timestamp`, its PK must become
composite `(id, timestamp)`. A single-column inbound FK cannot reference a composite
PK, so the only inbound FK — `commentmention.comment_id -> alertaudit.id`
(`fk_commentmention_alertaudit_cascade`, `ON DELETE CASCADE`) — must be dropped.

The cascade that this FK performed (deleting a comment's `commentmention` rows when
its `alertaudit` row is deleted) is now enforced in application code — see
`delete_alert` in keep-event-handler, which deletes the `commentmention` rows before
the `alertaudit` rows. The ORM models drop the FK and use an explicit relationship
`primaryjoin` instead (all three repos).

`alertaudit`'s only *outbound* FK (`tenant_id -> tenant.id`) is unaffected — a
partitioned table may reference other tables.

Revision ID: drop_commentmention_audit_fk
Revises: dedup_event_timestamp_index
Create Date: 2026-07-20
"""

from alembic import op

revision = "drop_commentmention_audit_fk"
down_revision = "dedup_event_timestamp_index"
branch_labels = None
depends_on = None

CONSTRAINT = "fk_commentmention_alertaudit_cascade"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    # IF EXISTS so the migration is safe even if the constraint was already dropped
    # manually in an environment.
    op.execute(
        f"ALTER TABLE commentmention DROP CONSTRAINT IF EXISTS {CONSTRAINT}"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    op.create_foreign_key(
        CONSTRAINT,
        "commentmention",
        "alertaudit",
        ["comment_id"],
        ["id"],
        ondelete="CASCADE",
    )
