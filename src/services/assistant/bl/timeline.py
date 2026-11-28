"""A conversation as the UI shows it again after a reload."""
import json
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import aliased
from sqlmodel import Session

from src.services.assistant import navigation
from src.services.assistant.bl import drafts_bl
from src.services.assistant.bl.conversations_bl import owned, turn_is_active
from src.services.assistant.bl.projection import visible_text
from src.services.assistant.core.db import get_session
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.api.conversation import (
    TIMELINE_LIMIT,
    ConversationView,
    TimelineDraft,
    TimelineLink,
    TimelineMessage,
)
from src.services.assistant.models.db import Conversation, Draft, Message
from src.services.assistant.models.identity import Identity
from src.services.assistant.tools import open_page

# How open_page's stored result begins (encode_result: compact, keys in order).
LINK_RESULT_PREFIX = '{"status":"link_ready",'


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
                if (item := _item(message, drafts, identity)) is not None
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
                    _link_row(),
                ),
            )
            .order_by(Message.seq.desc())
            .limit(TIMELINE_LIMIT + 1)
        )
    )


def _link_row() -> sa.ColumnElement[bool]:
    """A tool row of open_page that made a link: its result leads with link_ready,
    and the call it answers, in the same conversation, names open_page."""
    asked = aliased(Message)
    call = sa.func.jsonb_build_array(
        sa.func.jsonb_build_object(
            sa.cast(sa.literal("id"), sa.Text),
            Message.tool_call_id,
            sa.cast(sa.literal("name"), sa.Text),
            sa.cast(sa.literal(open_page.NAME), sa.Text),
        )
    )
    return sa.and_(
        Message.role == "tool",
        Message.draft_id.is_(None),
        Message.tool_call_id.is_not(None),
        Message.content.startswith(LINK_RESULT_PREFIX, autoescape=True),
        sa.exists().where(
            asked.conversation_id == Message.conversation_id,
            asked.tenant_id == Message.tenant_id,
            asked.role == "assistant",
            asked.tool_calls.contains(call),
        ),
    )


def _item(
    message: Message, drafts: dict[uuid.UUID, Draft], identity: Identity
) -> TimelineMessage | TimelineDraft | TimelineLink | None:
    if message.role == "tool" and message.draft_id is None:
        return _link(message, identity)
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


def _link(message: Message, identity: Identity) -> TimelineLink | None:
    """The stored link, read again as this version reads paths: None when it cannot
    be read, or names a page the user may no longer open. Never raises."""
    try:
        link = json.loads(message.content or "").get("link")
        path, label = link["path"], link["label"]
    except (ValueError, AttributeError, KeyError, TypeError, RecursionError):
        return None
    parsed = navigation.parse(path)
    if parsed is None or not isinstance(label, str):
        return None
    page, param = parsed
    if (link.get("page"), link.get("param")) != parsed:
        return None
    if not open_page.may_open(identity, page):
        return None
    label = visible_text(label).strip()[: open_page.MAX_LABEL_CHARS]
    if not label:
        return None
    return TimelineLink(
        id=message.id,
        page=page,
        param=param,
        path=path,
        label=label,
        created_at=message.created_at,
    )
