from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field

from src.services.assistant.models.db.base import AssistantModel
from src.services.assistant.models.db.columns import tenant_fk, timestamp_now

MAX_TITLE_CHARS = 120


class Conversation(AssistantModel, table=True):
    """Owned by (tenant_id, created_by). Any other caller gets a 404."""

    __tablename__ = "assistant_conversation"
    __table_args__ = (
        # Redundant with the primary key: the target of the children's composite
        # foreign key, which ties a child's tenant to its conversation's.
        sa.UniqueConstraint(
            "id", "tenant_id", name="uq_assistant_conversation_id_tenant_id"
        ),
        sa.Index(
            "ix_assistant_conversation_owner", "tenant_id", "created_by", "updated_at"
        ),
        # Retention, deliberately cross-tenant.
        sa.Index("ix_assistant_conversation_updated_at", "updated_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(sa_column=tenant_fk())
    # The gateway identity's email, as whoami returns it.
    created_by: str = Field(sa_column=sa.Column(sa.Text(), nullable=False))
    title: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text()))
    # The turn lock. Stale after a timeout, so a crashed pod cannot hold it.
    active_turn_id: Optional[UUID] = Field(default=None)
    turn_started_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True))
    )
    created_at: Optional[datetime] = Field(default=None, sa_column=timestamp_now())
    # Bumped by every message of the conversation; drives retention.
    updated_at: Optional[datetime] = Field(default=None, sa_column=timestamp_now())
