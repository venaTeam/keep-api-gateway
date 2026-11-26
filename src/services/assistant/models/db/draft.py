from datetime import datetime
from typing import Any, Dict, Optional, get_args
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field

from src.services.assistant.models.api.draft import DraftState
from src.services.assistant.models.db.base import AssistantModel
from src.services.assistant.models.db.columns import jsonb, tenant_fk, timestamp_now

_STATES = ", ".join(f"'{state}'" for state in get_args(DraftState))


class Draft(AssistantModel, table=True):
    __tablename__ = "assistant_draft"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["assistant_conversation.id", "assistant_conversation.tenant_id"],
            name="fk_assistant_draft_conversation_tenant",
        ),
        sa.CheckConstraint(f"state IN ({_STATES})", name="ck_assistant_draft_state"),
        sa.Index("ix_assistant_draft_conversation", "conversation_id", "created_at"),
        # The expiry sweep, deliberately cross-tenant.
        sa.Index(
            "ix_assistant_draft_pending_expiry",
            "expires_at",
            postgresql_where=sa.text("state = 'pending'"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(sa_column=tenant_fk())
    conversation_id: UUID = Field(nullable=False)
    created_by: str = Field(sa_column=sa.Column(sa.Text(), nullable=False))
    # The name of the tool that made it; an open set owned by the tool registry.
    kind: str = Field(sa_column=sa.Column(sa.Text(), nullable=False))
    state: str = Field(
        default="pending",
        sa_column=sa.Column(sa.Text(), nullable=False, server_default="pending"),
    )
    # Scope hint at creation time. The gateway decides.
    permitted: bool = Field(
        default=True,
        sa_column=sa.Column(
            sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    payload: Dict[str, Any] = Field(sa_column=jsonb(nullable=False))
    outcome: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=jsonb(nullable=True)
    )
    # Always written as now() + ttl in SQL.
    expires_at: datetime = Field(
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False)
    )
    resolved_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True))
    )
    created_at: Optional[datetime] = Field(default=None, sa_column=timestamp_now())
    updated_at: Optional[datetime] = Field(default=None, sa_column=timestamp_now())
