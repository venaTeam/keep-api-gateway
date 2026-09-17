"""stop storing 'suppressed' as an alert's status when it comes from a dismissal

Dismissing an alert used to write BOTH `lastalert.dismiss_mode` and
`lastalert.status='suppressed'`. Because `status` is the user override every
reader coalesces over the provider's `alert.status`, the suppression was a stored
fact — and since nothing sweeps the table, an alert dismissed until a deadline
stayed suppressed forever once that deadline passed. `apply_dismiss_lifecycle`
even documents the intent ("it self-expires on its own clock") but no clock was
ever implemented.

Suppression is now DERIVED from dismiss_mode/dismissed_until on read
(`LastAlert.get_effective_status` and the matching SQL in
repositories/alerts.py), so `status` is free to hold what the alert reverts to
when a dismissal lapses. This clears the redundant stored value on rows written
under the old behaviour; leaving it would pin those alerts to 'suppressed' even
after their deadline, which is the exact bug being fixed.

DELIBERATELY NARROW: only rows where status='suppressed' AND a dismiss_mode is
set. An alert suppressed for some other reason (a maintenance window, a direct
status write with no dismissal) has no dismiss_mode and keeps its status — there
would be nothing to derive the suppression from.

Reversible in shape but not in fact: `downgrade` re-stamps 'suppressed' onto
dismissed rows, which restores the old never-expiring behaviour for them, but it
cannot distinguish rows that were already 'suppressed' for another reason.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "derive_alert_suppression"
down_revision = "purge_orphan_incident_audit"
branch_labels = None
depends_on = None

_CLEAR_SQL = sa.text(
    """
    UPDATE lastalert
    SET status = NULL
    WHERE status = 'suppressed'
      AND dismiss_mode IS NOT NULL
    """
)

_RESTORE_SQL = sa.text(
    """
    UPDATE lastalert
    SET status = 'suppressed'
    WHERE dismiss_mode IS NOT NULL
      AND status IS NULL
    """
)


def upgrade() -> None:
    result = op.get_bind().execute(_CLEAR_SQL)
    print(f"cleared stored 'suppressed' status on {result.rowcount} dismissed alerts")


def downgrade() -> None:
    op.get_bind().execute(_RESTORE_SQL)
