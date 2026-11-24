"""A conversation as the UI shows it again after a reload."""
import uuid

import sqlalchemy as sa
from sqlmodel import Session

from src.services.assistant.bl import drafts_bl
from src.services.assistant.bl.conversations_bl import owned, turn_is_active
from src.services.assistant.core.db import get_session
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.api.conversation import (
    TIMELINE_LIMIT,
    ConversationView,
    TimelineDraft,
    TimelineMessage,
)
from src.services.assistant.models.db import Conversation, Draft, Message
from src.services.assistant.models.identity import Identity


def load(identity: Identity, conversation_id: uuid.UUID) -> ConversationView:
    """Raises conversation_not_found."""
    with get_session() as session:
        row = session.execute(
            sa.select(
                Conversation,
                sa.func.coalesce(turn_is_active(), sa.false()).label("active"),
            ).where(
                Conversation.id == conversation_id,
                *owned(identity.tenant_id, identity.email),
            )
        ).one_or_none()
        if row is None:
            raise AssistantError("conversation_not_found")
        conversation, active = row
        newest = _newest_messages(session, conversation)
        shown = newest[:TIMELINE_LIMIT][::-1]
        drafts = drafts_bl.load_many(
            session, identity, [m.draft_id for m in shown if m.role == "tool"]
        )
        return ConversationView(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            turn_in_progress=active,
            has_more=len(newest) > TIMELINE_LIMIT,
            items=[
                item
                for message in shown
                if (item := _item(message, drafts)) is not None
            ],
        )


def _newest_messages(session: Session, conversation: Conversation) -> list[Message]:
    """What can be seen, newest first, one more than is shown. A draft stands where
    the tool made it."""
    return list(
        session.scalars(
            sa.select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.tenant_id == conversation.tenant_id,
                sa.or_(
                    Message.role == "user",
                    # Not the rows that only carry tool calls.
                    sa.and_(Message.role == "assistant", Message.content != ""),
                    sa.and_(Message.role == "tool", Message.draft_id.is_not(None)),
                ),
            )
            .order_by(Message.seq.desc())
            .limit(TIMELINE_LIMIT + 1)
        )
    )


def _item(
    message: Message, drafts: dict[uuid.UUID, Draft]
) -> TimelineMessage | TimelineDraft | None:
    if message.role != "tool":
        return TimelineMessage(
            id=message.id,
            role=message.role,
            content=message.content or "",
            status=message.status,
            created_at=message.created_at,
        )
    draft = drafts.get(message.draft_id)
    return None if draft is None else TimelineDraft(draft=drafts_bl.to_view(draft))
