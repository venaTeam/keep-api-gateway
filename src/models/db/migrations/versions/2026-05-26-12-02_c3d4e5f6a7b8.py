"""phase1: backfill lastalert tracking + user columns from alert

Revision ID: c3d4e5f6a7b8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-26 12:02:00.000000

Phase 1 / task 1.3.

Copies the misplaced tracking/user fields from the alert row that lastalert
currently points to (lastalert.alert_id = alert.id) into the new lastalert
columns. Source columns are snake_case (post Migration 1); last_received is a
straight timestamptz->timestamptz copy (no cast).

``dismissed_until`` is intentionally NOT backfilled here: alert.dismiss_until is
vestigial (always NULL on insert per spec F21). The authoritative dismiss state
is applied in task 1.4 from alertenrichment.

Batched with keyset pagination over the lastalert PK (tenant_id, fingerprint),
committing each batch so it is safe to run on a live table (NF5). Idempotent.
PostgreSQL only (Migration 1 / backfill SQL is Postgres-specific).
"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None

BATCH_SIZE = 1000

_NEXT_KEY_SQL = text(
    """
    SELECT tenant_id, fingerprint
    FROM lastalert
    WHERE (tenant_id, fingerprint) > (:t0, :f0)
    ORDER BY tenant_id, fingerprint
    LIMIT :batch
    """
)

_UPDATE_SQL = text(
    """
    UPDATE lastalert la SET
        last_received = a.last_received,
        firing_counter = COALESCE(a.firing_counter, 0),
        unresolved_counter = COALESCE(a.unresolved_counter, 0),
        started_at = a.started_at,
        firing_start_time = a.firing_start_time,
        firing_start_time_since_last_resolved = a.firing_start_time_since_last_resolved,
        assignee = a.assignee,
        note = a.note
    FROM alert a
    WHERE la.alert_id = a.id
      AND (la.tenant_id, la.fingerprint) > (:t0, :f0)
      AND (la.tenant_id, la.fingerprint) <= (:t1, :f1)
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    t0, f0 = "", ""
    while True:
        rows = conn.execute(
            _NEXT_KEY_SQL, {"t0": t0, "f0": f0, "batch": BATCH_SIZE}
        ).fetchall()
        if not rows:
            break
        t1, f1 = rows[-1][0], rows[-1][1]
        conn.execute(_UPDATE_SQL, {"t0": t0, "f0": f0, "t1": t1, "f1": f1})
        conn.commit()
        t0, f0 = t1, f1


def downgrade() -> None:
    # Data-only backfill; reset the copied columns to NULL / default.
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.execute(
        text(
            """
            UPDATE lastalert SET
                last_received = NULL,
                firing_counter = 0,
                unresolved_counter = 0,
                started_at = NULL,
                firing_start_time = NULL,
                firing_start_time_since_last_resolved = NULL,
                assignee = NULL,
                note = NULL
            """
        )
    )
    conn.commit()
