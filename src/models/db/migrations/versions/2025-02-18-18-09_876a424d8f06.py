"""Extend dismissed enrichments with SUPPRESSED status

Revision ID: 876a424d8f06
Revises: 8176d7153747
Create Date: 2025-02-18 18:09:40.656808

"""

import json

from alembic import op
from sqlalchemy import Column, MetaData, String, Table, and_, null, update
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlmodel import JSON, Session

from src.repositories.db_utils import get_json_extract_field
from src.models.alert import AlertStatus

# revision identifiers, used by Alembic.
revision = "876a424d8f06"
down_revision = "8176d7153747"
branch_labels = None
depends_on = None


# Self-contained Core table reflection so this historical migration keeps
# running after the live `AlertEnrichment` ORM model is removed. Migrations must
# not depend on the current model layer.
_alertenrichment = Table(
    "alertenrichment",
    MetaData(),
    Column("id", String, primary_key=True),
    Column("alert_fingerprint", String, unique=True),
    Column("enrichments", JSON().with_variant(PG_JSONB, "postgresql")),
)


def populate_db():
    session = Session(op.get_bind())

    enrichments_col = _alertenrichment.c.enrichments
    dismissed_field = get_json_extract_field(session, enrichments_col, "dismissed")
    status_field = get_json_extract_field(session, enrichments_col, "status")

    rows = session.execute(
        _alertenrichment.select().where(
            and_(
                dismissed_field.in_(["true", "True"]),
                status_field.is_(null()),
            )
        )
    ).all()

    for row in rows:
        updated = dict(row.enrichments or {})
        updated["status"] = AlertStatus.SUPPRESSED.value
        session.execute(
            update(_alertenrichment)
            .where(_alertenrichment.c.id == row.id)
            .values(enrichments=updated)
        )
    session.commit()

def upgrade() -> None:
    populate_db()


def downgrade() -> None:
    pass

