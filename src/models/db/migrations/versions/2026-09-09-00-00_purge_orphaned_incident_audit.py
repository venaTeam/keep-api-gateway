"""purge alertaudit rows left behind by incidents that no longer exist

Incident audit rows are keyed by the incident's UUID in `alertaudit.fingerprint`
with no foreign key, so they used to outlive the incident. Back when deleting an
incident only flipped its status to `deleted` the row was still there, so the
audit trail stayed reachable; once delete became a real DELETE the rows became
unreachable, because every read path looks them up through a live incident id.
`delete_incident_by_id` now removes them as part of the delete. This clears the
ones stranded before that.

DELIBERATELY NARROW. A row is only removed when all three hold:

  1. `fingerprint` parses as a UUID — incident audit keys are UUID strings,
     alert audit keys are provider fingerprints.
  2. No `incident` row has that id, in any tenant.
  3. No `alert` or `lastalert` row has that fingerprint. Nothing stops a provider
     from emitting a UUID-shaped fingerprint, and an alert's audit history must
     survive its incident being deleted.

Comment @mentions attached to those audit rows go too — they reference
`alertaudit.id` (the FK was dropped in `drop_commentmention_audit_fk`, so this is
not cascaded for us) and would otherwise be the next generation of orphans.

NOT REVERSIBLE: the rows are audit history with no other source.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "purge_orphan_incident_audit"
down_revision = "strip_incident_status_enrich"
branch_labels = None
depends_on = None

# Postgres-only: the UUID regex and the cast below are dialect-specific. Other
# dialects keep the orphans; they are inert, and the application path above
# prevents new ones regardless of dialect.
_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

_ORPHAN_AUDIT_IDS = """
    SELECT aa.id
    FROM alertaudit aa
    WHERE aa.fingerprint ~ :uuid_re
      AND NOT EXISTS (
          SELECT 1 FROM incident i WHERE i.id::text = aa.fingerprint
      )
      AND NOT EXISTS (
          SELECT 1 FROM alert a WHERE a.fingerprint = aa.fingerprint
      )
      AND NOT EXISTS (
          SELECT 1 FROM lastalert la WHERE la.fingerprint = aa.fingerprint
      )
"""

_DELETE_MENTIONS = sa.text(
    f"""
    DELETE FROM commentmention
    WHERE comment_id IN ({_ORPHAN_AUDIT_IDS})
    """
)

_DELETE_AUDIT = sa.text(
    f"""
    DELETE FROM alertaudit
    WHERE id IN ({_ORPHAN_AUDIT_IDS})
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # Mentions first, while the audit rows they point at still exist to be found.
    conn.execute(_DELETE_MENTIONS, {"uuid_re": _UUID_RE})
    result = conn.execute(_DELETE_AUDIT, {"uuid_re": _UUID_RE})
    print(f"purged {result.rowcount} orphaned incident audit rows")


def downgrade() -> None:
    # Audit history; nothing to restore it from.
    pass
