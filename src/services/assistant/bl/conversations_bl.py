"""Conversations and the turn lock.

A turn owns its conversation through `active_turn_id`. Every write of a turn checks
that it still does: a turn that was slower than TURN_STALE_SECONDS may have been
replaced, and must not write into the newer one.

Locks are always taken in the order conversation, draft, message.
"""
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlmodel import Session

from src.services.assistant.bl import drafts_bl, rate_limit
from src.services.assistant.core import metrics
from src.services.assistant.core.db import get_session
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.db import Conversation, Message
from src.services.assistant.models.db.conversation import MAX_TITLE_CHARS
from src.services.assistant.models.identity import Identity
from src.services.assistant.tools.base import NewDraft

logger = logging.getLogger(__name__)

# Longer than any turn can run (the deadline plus its cleanup): the lock of a worker
# that died ends by itself.
TURN_STALE_SECONDS = 120
MAX_ACTIVE_TURNS = 2
_ACTIVE_TURNS_RETRY_SECONDS = 5


class TurnLost(Exception):
    """The turn no longer owns its conversation, or the conversation is gone."""


@dataclass(frozen=True)
class TurnRef:
    tenant_id: str
    created_by: str
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    user_message_id: uuid.UUID


@dataclass(frozen=True)
class NewMessage:
    """A row to store. Its place in the conversation is the order of the inserts."""

    role: str
    content: str | None
    id: uuid.UUID
    status: str = "complete"
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    draft_id: uuid.UUID | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def storable(value: str) -> str:
    """Model output as Postgres can hold it: no NUL, no lone surrogate."""
    return value.replace("\x00", "").encode("utf-8", "replace").decode()


def turn_is_active() -> sa.ColumnElement[bool]:
    return sa.and_(
        Conversation.active_turn_id.is_not(None),
        Conversation.turn_started_at
        > sa.func.now() - timedelta(seconds=TURN_STALE_SECONDS),
    )


def owned(tenant_id: str, created_by: str) -> tuple[sa.ColumnElement[bool], ...]:
    return Conversation.tenant_id == tenant_id, Conversation.created_by == created_by


def start_turn(
    identity: Identity, conversation_id: uuid.UUID | None, message: str
) -> TurnRef:
    """Take the turn lock and store the user's message, all or nothing.

    Raises conversation_not_found, rate_limited, turn_in_progress or
    too_many_active_turns.
    """
    with get_session() as session, session.begin():
        # The counts below decide about this insert: one at a time per user.
        session.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(
                        f"assistant:{identity.tenant_id}:{identity.email}", 0
                    )
                )
            )
        )
        if conversation_id is not None:
            _require_owned(session, identity, conversation_id)
        rate_limit.check(session, identity.tenant_id, identity.email)
        _check_active_turns(session, identity, conversation_id)

        turn_id = uuid.uuid4()
        if conversation_id is None:
            conversation_id = _create(session, identity, turn_id, message)
        else:
            _take_lock(session, identity, conversation_id, turn_id)
        user_message_id = uuid.uuid4()
        _insert(
            session,
            identity.tenant_id,
            conversation_id,
            NewMessage("user", message, id=user_message_id),
        )
    return TurnRef(
        tenant_id=identity.tenant_id,
        created_by=identity.email,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_message_id=user_message_id,
    )


def _require_owned(
    session: Session, identity: Identity, conversation_id: uuid.UUID
) -> None:
    found = session.scalar(
        sa.select(Conversation.id).where(
            Conversation.id == conversation_id,
            *owned(identity.tenant_id, identity.email),
        )
    )
    if found is None:
        raise AssistantError("conversation_not_found")


def _check_active_turns(
    session: Session, identity: Identity, conversation_id: uuid.UUID | None
) -> None:
    active = set(
        session.scalars(
            sa.select(Conversation.id).where(
                *owned(identity.tenant_id, identity.email), turn_is_active()
            )
        )
    )
    if conversation_id in active:
        raise _turn_in_progress()
    if len(active) >= MAX_ACTIVE_TURNS:
        metrics.rate_limited_total.inc()
        raise AssistantError(
            "too_many_active_turns",
            headers={"Retry-After": str(_ACTIVE_TURNS_RETRY_SECONDS)},
        )


def _turn_in_progress() -> AssistantError:
    metrics.turn_lock_conflicts_total.inc()
    return AssistantError("turn_in_progress")


def _create(
    session: Session, identity: Identity, turn_id: uuid.UUID, message: str
) -> uuid.UUID:
    return session.scalar(
        sa.insert(Conversation)
        .values(
            id=uuid.uuid4(),
            tenant_id=identity.tenant_id,
            created_by=identity.email,
            title=" ".join(message.split())[:MAX_TITLE_CHARS],
            active_turn_id=turn_id,
            turn_started_at=sa.func.now(),
        )
        .returning(Conversation.id)
    )


def _take_lock(
    session: Session,
    identity: Identity,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> None:
    taken = session.scalar(
        sa.update(Conversation)
        .where(
            Conversation.id == conversation_id,
            *owned(identity.tenant_id, identity.email),
            sa.not_(sa.func.coalesce(turn_is_active(), sa.false())),
        )
        .values(
            active_turn_id=turn_id,
            turn_started_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
        .returning(Conversation.id)
    )
    if taken is None:
        raise _turn_in_progress()


def _insert(
    session: Session, tenant_id: str, conversation_id: uuid.UUID, message: NewMessage
) -> None:
    session.execute(
        sa.insert(Message).values(
            id=message.id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=message.role,
            status=message.status,
            content=None if message.content is None else storable(message.content),
            tool_calls=message.tool_calls
            and [
                {key: storable(value) for key, value in call.items()}
                for call in message.tool_calls
            ],
            tool_call_id=message.tool_call_id and storable(message.tool_call_id),
            draft_id=message.draft_id,
            model=message.model,
            prompt_tokens=message.prompt_tokens,
            completion_tokens=message.completion_tokens,
        )
    )


def _update_if_owned(session: Session, turn: TurnRef, **values: Any) -> bool:
    """Bump the conversation, locking its row; False when the turn lost it."""
    return (
        session.scalar(
            sa.update(Conversation)
            .where(
                Conversation.id == turn.conversation_id,
                *owned(turn.tenant_id, turn.created_by),
                Conversation.active_turn_id == turn.turn_id,
            )
            .values(updated_at=sa.func.now(), **values)
            .returning(Conversation.id)
        )
        is not None
    )


def add_messages(
    turn: TurnRef, messages: Sequence[NewMessage], draft: NewDraft | None = None
) -> DraftView | None:
    """Store rows of a running turn, and the draft one of them belongs to. Raises
    TurnLost."""
    view = None
    with get_session() as session, session.begin():
        if not _update_if_owned(session, turn):
            raise TurnLost()
        if draft is not None:
            view = drafts_bl.to_view(
                drafts_bl.insert_draft(
                    session,
                    tenant_id=turn.tenant_id,
                    created_by=turn.created_by,
                    conversation_id=turn.conversation_id,
                    draft=draft,
                )
            )
        for message in messages:
            _insert(session, turn.tenant_id, turn.conversation_id, message)
    return view


def end_turn(turn: TurnRef, partial: NewMessage | None = None) -> bool:
    """Release the turn lock, storing what a cut-short answer had so far. False when
    the turn had lost it already: nothing is stored then."""
    with get_session() as session, session.begin():
        if not _update_if_owned(
            session, turn, active_turn_id=None, turn_started_at=None
        ):
            return False
        if partial is not None:
            _insert(session, turn.tenant_id, turn.conversation_id, partial)
    return True
