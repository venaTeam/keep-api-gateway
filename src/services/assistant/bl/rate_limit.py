"""How many messages a user may send. Counted in the database, so that every worker and
pod sees the same numbers."""
import math
from datetime import timedelta

import sqlalchemy as sa
from sqlmodel import Session

from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.db import Conversation, Message

_MINUTE = timedelta(minutes=1)
_DAY = timedelta(days=1)


def check(session: Session, tenant_id: str, created_by: str) -> None:
    """Raises `rate_limited`. The caller serializes the messages of one user."""
    now = sa.func.now()
    in_minute = Message.created_at > now - _MINUTE

    def seconds_left(oldest, window: timedelta):
        return sa.extract("epoch", oldest + window - now)

    minute, day, minute_left, day_left = session.execute(
        sa.select(
            sa.func.count().filter(in_minute),
            sa.func.count(),
            seconds_left(sa.func.min(Message.created_at).filter(in_minute), _MINUTE),
            seconds_left(sa.func.min(Message.created_at), _DAY),
        )
        .select_from(Conversation)
        .join(
            Message,
            sa.and_(
                Message.conversation_id == Conversation.id,
                Message.tenant_id == Conversation.tenant_id,
            ),
        )
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.created_by == created_by,
            # Every message bumps its conversation: the older ones need no look.
            Conversation.updated_at > now - _DAY,
            Message.role == "user",
            Message.created_at > now - _DAY,
        )
    ).one()
    settings = get_settings()
    if day >= settings.rate_limit_per_day:
        _refuse(day_left)
    if minute >= settings.rate_limit_per_minute:
        _refuse(minute_left)


def _refuse(seconds_left) -> None:
    metrics.rate_limited_total.inc()
    # None: a limit of zero, with no message to wait for.
    wait = 60 if seconds_left is None else max(1, math.ceil(seconds_left))
    raise AssistantError("rate_limited", headers={"Retry-After": str(wait)})
