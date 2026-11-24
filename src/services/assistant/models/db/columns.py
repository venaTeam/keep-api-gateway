"""Column shapes shared by the assistant tables."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


def tenant_fk() -> sa.Column:
    # The gateway's `tenant` is in another MetaData. Only use_alter keeps a flush
    # from looking the target up; the constraint itself is the same.
    return sa.Column(
        sa.String(), sa.ForeignKey("tenant.id", use_alter=True), nullable=False
    )


def timestamp_now() -> sa.Column:
    """Set by the database, so an application clock never matters."""
    return sa.Column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def jsonb(nullable: bool) -> sa.Column:
    # Without none_as_null, None is stored as the JSON value `null`.
    return sa.Column(JSONB(none_as_null=True), nullable=nullable)
