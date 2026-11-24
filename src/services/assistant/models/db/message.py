from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field

from src.services.assistant.models.db.base import AssistantModel
from src.services.assistant.models.db.columns import jsonb, tenant_fk, timestamp_now


class Message(AssistantModel, table=True):
    """One row of a conversation. The order is `seq`: now() is constant inside a
    transaction, so `created_at` cannot order rows written together."""

    __tablename__ = "assistant_message"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["assistant_conversation.id", "assistant_conversation.tenant_id"],
            name="fk_assistant_message_conversation_tenant",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool', 'event')",
            name="ck_assistant_message_role",
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'interrupted', 'error')",
            name="ck_assistant_message_status",
        ),
        sa.Index("ix_assistant_message_conversation", "conversation_id", "seq"),
        # The ON DELETE SET NULL lookup.
        sa.Index(
            "ix_assistant_message_draft",
            "draft_id",
            postgresql_where=sa.text("draft_id IS NOT NULL"),
        ),
        # The per-user rate limit.
        sa.Index(
            "ix_assistant_message_user_rate",
            "conversation_id",
            "created_at",
            postgresql_where=sa.text("role = 'user'"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(sa_column=tenant_fk())
    conversation_id: UUID = Field(nullable=False)
    seq: Optional[int] = Field(
        default=None,
        sa_column=sa.Column(sa.BigInteger(), sa.Identity(), nullable=False),
    )
    role: str = Field(sa_column=sa.Column(sa.Text(), nullable=False))
    status: str = Field(
        default="complete",
        sa_column=sa.Column(sa.Text(), nullable=False, server_default="complete"),
    )
    # User or assistant text, the tool result as JSON, or the event text.
    content: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text()))
    # Assistant rows: [{"id", "name", "arguments"}].
    tool_calls: Optional[List[Dict[str, Any]]] = Field(
        default=None, sa_column=jsonb(nullable=True)
    )
    tool_call_id: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text()))
    draft_id: Optional[UUID] = Field(
        default=None,
        sa_column=sa.Column(
            sa.Uuid(),
            sa.ForeignKey("assistant_draft.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    model: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text()))
    prompt_tokens: Optional[int] = Field(default=None)
    completion_tokens: Optional[int] = Field(default=None)
    created_at: Optional[datetime] = Field(default=None, sa_column=timestamp_now())
