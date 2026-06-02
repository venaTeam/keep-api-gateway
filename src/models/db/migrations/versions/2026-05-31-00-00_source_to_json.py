"""alert source column to JSON

Revision ID: source_to_json
Revises: alert_standardization
Create Date: 2026-05-31 00:00:00.000000

"""

import ast
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

revision = "source_to_json"
down_revision = "alert_standardization"
branch_labels = None
depends_on = None


BATCH = 5000


def _parse_source(raw):
    """Coerce a legacy VARCHAR(255) `source` value into a list[str].

    Existing rows can be in three shapes depending on how SQLAlchemy bound the
    Python `list` to a String column:
      - JSON repr e.g. '["prometheus"]'      (postgres extracted via event->'source')
      - Python repr e.g. "['prometheus']"    (str(list) coercion on some drivers)
      - bare string  e.g. 'prometheus'       (older rows that were already scalar)
    """
    if raw is None or raw == "":
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            return [v]
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        v = ast.literal_eval(raw)
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            return [v]
    except (ValueError, SyntaxError):
        pass
    return [raw]


def _backfill(conn, alert_t, src_col, dst_col, transform):
    ids = [
        r[0]
        for r in conn.execute(
            sa.select(alert_t.c.id).where(alert_t.c[src_col].isnot(None))
        ).fetchall()
    ]
    for i in range(0, len(ids), BATCH):
        chunk = ids[i : i + BATCH]
        rows = conn.execute(
            sa.select(alert_t.c.id, alert_t.c[src_col]).where(
                alert_t.c.id.in_(chunk)
            )
        ).fetchall()
        params = [
            {"_id": r.id, "_val": transform(r[1])} for r in rows
        ]
        if not params:
            continue
        stmt = (
            sa.update(alert_t)
            .where(alert_t.c.id == sa.bindparam("_id"))
            .values({dst_col: sa.bindparam("_val")})
        )
        conn.execute(stmt, params)


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "alert",
        sa.Column(
            "source_new",
            sa.JSON().with_variant(PG_JSONB, "postgresql"),
            nullable=True,
        ),
    )

    meta = sa.MetaData()
    alert_t = sa.Table("alert", meta, autoload_with=conn)

    _backfill(conn, alert_t, "source", "source_new", _parse_source)

    with op.batch_alter_table("alert") as batch:
        batch.drop_column("source")
        batch.alter_column("source_new", new_column_name="source")


def downgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "alert", sa.Column("source_old", sa.String(length=255), nullable=True)
    )

    meta = sa.MetaData()
    alert_t = sa.Table("alert", meta, autoload_with=conn)

    def to_scalar(v):
        if isinstance(v, list):
            return v[0] if v else None
        if isinstance(v, str):
            return v
        return None

    _backfill(conn, alert_t, "source", "source_old", to_scalar)

    with op.batch_alter_table("alert") as batch:
        batch.drop_column("source")
        batch.alter_column("source_old", new_column_name="source")
