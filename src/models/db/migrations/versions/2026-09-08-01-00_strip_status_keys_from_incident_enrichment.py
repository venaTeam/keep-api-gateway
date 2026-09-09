"""strip status/dismiss keys out of incidentenrichment.enrichments

`status`, `dismiss_mode` and `dismissed_until` are owned by the typed columns on
`incident` and written only by POST /incidents/{id}/status. They used to be
accepted as free-form enrichments too, and the CEL `status` mapping coalesced the
JSONB copy AHEAD of `incident.status` — so an enrichment could silently shadow an
incident's real status, and a "dismiss until <date>" sent through the enrich route
never reached the columns that the suppression derivation reads.

The read paths no longer consult these keys (`incident_field_configurations` and
`IncidentDto.from_db_incident`) and the write path now rejects them
(`INCIDENT_STATUS_OWNED_KEYS`). This removes the copies already stored, so the
rows don't keep a stale shadow of a status that nothing honours.

Only these three keys are touched — notes, tickets and every other enrichment key
in the same blob are left exactly as they are. Rows whose blob becomes empty are
kept: the row's existence is not itself meaningful, and deleting it would be a
wider change than this migration needs to make.

NOT REVERSIBLE: the discarded values are redundant with the typed columns by
definition, and there is nothing to restore them from.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "strip_incident_status_enrich"
down_revision = "incident_dismiss_columns"
branch_labels = None
depends_on = None

# jsonb_exists(col, key) rather than the `?|` operator: `?` collides with DBAPI
# parameter parsing. enrichments is cast ::jsonb since some existing databases
# store the column as `json`, which jsonb_exists will not accept.
_STRIP_SQL = sa.text(
    """
    UPDATE incidentenrichment
    SET enrichments = (enrichments::jsonb)
        - 'status' - 'dismiss_mode' - 'dismissed_until'
    WHERE jsonb_exists(enrichments::jsonb, 'status')
       OR jsonb_exists(enrichments::jsonb, 'dismiss_mode')
       OR jsonb_exists(enrichments::jsonb, 'dismissed_until')
    """
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        # The JSONB key-delete operators below are Postgres-only. Other dialects
        # run the app against a `json` column where these keys are simply ignored
        # by every reader, so leaving them in place is harmless.
        return
    op.execute(_STRIP_SQL)


def downgrade() -> None:
    # The stripped keys duplicated incident.status / incident.dismiss_mode /
    # incident.dismissed_until; there is no source to rebuild them from.
    pass
