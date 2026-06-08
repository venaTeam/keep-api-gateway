"""create the incidentenrichment table and backfill it from alertenrichment

Incident enrichment previously shared the dual-use ``alertenrichment`` table,
storing the incident UUID in the ``alert_fingerprint`` text column. This
introduces a dedicated incident-scoped ``incidentenrichment`` table keyed on a
real ``incident_id`` foreign key, then backfills every ``alertenrichment`` row
whose ``alert_fingerprint`` matches an existing ``incident.id`` (compared as
text) for the same tenant.

The backfill generates a fresh python-side ``uuid4`` per row and inserts via a
parameterized statement — no dependency on ``gen_random_uuid()`` — and stays
portable across the SQLite test engine and the Postgres production engine.

Revision ID: create_incident_enrichment
Revises: alert_received_at
Create Date: 2026-06-07
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "create_incident_enrichment"
down_revision = "alert_received_at"
branch_labels = None
depends_on = None


def _enrichments_json_type():
    """JSONB on Postgres, JSON elsewhere (SQLite test engine)."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "incidentenrichment",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incident.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("enrichments", _enrichments_json_type(), nullable=True),
    )

    _backfill_from_alertenrichment()


def _backfill_from_alertenrichment() -> None:
    """Copy incident-scoped rows out of alertenrichment.

    A row qualifies when its ``alert_fingerprint`` equals an existing
    ``incident.id`` (compared as text) within the same tenant. Each match is
    inserted with a fresh uuid4 primary key, preserving tenant, timestamp and
    enrichments, and pointing ``incident_id`` at the matched incident.

    Core ``Table`` constructs (not raw text) drive the typed parameter binding so
    UUID primary keys and the JSON/JSONB ``enrichments`` value serialize
    correctly on both SQLite and Postgres.
    """
    bind = op.get_bind()
    metadata = sa.MetaData()

    alertenrichment = sa.Table(
        "alertenrichment",
        metadata,
        sa.Column("tenant_id", sa.String()),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("alert_fingerprint", sa.String()),
        sa.Column("enrichments", _enrichments_json_type()),
    )
    # ``incident.id`` is read as text here purely to drive the text-equality join
    # against ``alert_fingerprint`` and to feed the typed ``incident_id`` insert
    # column below — avoids coupling this backfill to the UUID storage form,
    # which differs between SQLite and Postgres.
    incident = sa.Table(
        "incident",
        metadata,
        sa.Column("id", sa.String()),
        sa.Column("tenant_id", sa.String()),
    )
    incidentenrichment = sa.Table(
        "incidentenrichment",
        metadata,
        sa.Column("id", sa.Uuid()),
        sa.Column("tenant_id", sa.String()),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("incident_id", sa.Uuid()),
        sa.Column("enrichments", _enrichments_json_type()),
    )

    select_matches = sa.select(
        alertenrichment.c.tenant_id,
        alertenrichment.c.timestamp,
        alertenrichment.c.enrichments,
        incident.c.id.label("incident_id"),
    ).select_from(
        alertenrichment.join(
            incident,
            sa.and_(
                incident.c.tenant_id == alertenrichment.c.tenant_id,
                sa.cast(incident.c.id, sa.Text)
                == alertenrichment.c.alert_fingerprint,
            ),
        )
    )

    matched_rows = bind.execute(select_matches).mappings().all()
    if not matched_rows:
        return

    bind.execute(
        incidentenrichment.insert(),
        [
            {
                "id": uuid.uuid4(),
                "tenant_id": row["tenant_id"],
                # ``timestamp`` is NOT NULL on the target; default any legacy
                # NULL to "now" rather than fail the backfill.
                "timestamp": row["timestamp"] or datetime.utcnow(),
                # ``incident_id`` is read as text (dialect-neutral join) but the
                # target column is a typed UUID — coerce before insert.
                "incident_id": uuid.UUID(str(row["incident_id"])),
                "enrichments": row["enrichments"],
            }
            for row in matched_rows
        ],
    )


def downgrade() -> None:
    op.drop_table("incidentenrichment")
