"""phase1: backfill lastalert enrichment overrides from alertenrichment

Revision ID: c4d5e6f7a8b9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-26 12:03:00.000000

Phase 1 / task 1.4.

Applies user enrichment overrides on top of the task 1.3 values, reading the
JSONB alertenrichment.enrichments per fingerprint:

  - status   <- enrichments->>'status'
  - assignee <- enrichments->>'assignee'
  - note     <- enrichments->>'note'  (only when non-empty -> note-preservation guard)
  - dismiss translation:
        dismissUntil present  -> status='suppressed', dismiss_mode='dismiss_until',
                                 dismissed_until = dismissUntil::timestamptz
        else dismissed='true' -> status='suppressed', dismiss_mode='permanent'
  - disposable_* and all other dynamic custom keys are DISCARDED (Spec Option A).
  - status_disposable stays FALSE (no reliable source in old data).

Notes:
  - jsonb_exists(col, key) is used instead of the ``?`` operator because ``?``
    collides with DBAPI parameter parsing.
  - Incident-enrichment rows (UUID-keyed alert_fingerprint matching no lastalert)
    update nothing -> no incident data migration needed (feature unused).
  - Keyset pagination over id::text keeps batching agnostic to UUID storage.
  - Batches commit individually (NF5). PostgreSQL only.
"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "c4d5e6f7a8b9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

BATCH_SIZE = 1000

_NEXT_KEY_SQL = text(
    """
    SELECT id::text AS idt
    FROM alertenrichment
    WHERE id::text > :c0
    ORDER BY id::text
    LIMIT :batch
    """
)

_UPDATE_SQL = text(
    """
    UPDATE lastalert la SET
        status = CASE
            WHEN jsonb_exists(ae.enrichments, 'status') THEN ae.enrichments->>'status'
            WHEN jsonb_exists(ae.enrichments, 'dismissUntil')
                 AND COALESCE(ae.enrichments->>'dismissUntil', '') <> '' THEN 'suppressed'
            WHEN COALESCE(ae.enrichments->>'dismissed', '') = 'true' THEN 'suppressed'
            ELSE la.status
        END,
        dismiss_mode = CASE
            WHEN jsonb_exists(ae.enrichments, 'dismissUntil')
                 AND COALESCE(ae.enrichments->>'dismissUntil', '') <> '' THEN 'dismiss_until'
            WHEN COALESCE(ae.enrichments->>'dismissed', '') = 'true' THEN 'permanent'
            ELSE la.dismiss_mode
        END,
        dismissed_until = CASE
            WHEN jsonb_exists(ae.enrichments, 'dismissUntil')
                 AND ae.enrichments->>'dismissUntil' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                THEN (ae.enrichments->>'dismissUntil')::timestamptz
            ELSE la.dismissed_until
        END,
        assignee = CASE
            WHEN jsonb_exists(ae.enrichments, 'assignee') THEN ae.enrichments->>'assignee'
            ELSE la.assignee
        END,
        note = CASE
            WHEN COALESCE(ae.enrichments->>'note', '') <> '' THEN ae.enrichments->>'note'
            ELSE la.note
        END
    FROM alertenrichment ae
    WHERE ae.tenant_id = la.tenant_id
      AND ae.alert_fingerprint = la.fingerprint
      AND ae.id::text > :c0
      AND ae.id::text <= :c1
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    c0 = ""
    while True:
        rows = conn.execute(_NEXT_KEY_SQL, {"c0": c0, "batch": BATCH_SIZE}).fetchall()
        if not rows:
            break
        c1 = rows[-1][0]
        conn.execute(_UPDATE_SQL, {"c0": c0, "c1": c1})
        conn.commit()
        c0 = c1


def downgrade() -> None:
    # The task 1.3 downgrade already resets these columns to NULL/default;
    # nothing additional to undo here.
    pass
