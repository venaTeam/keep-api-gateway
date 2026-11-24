"""Deletes what is older than the retention period. Deliberately across tenants.

Blocking: runs in a thread. Every transaction is short and takes the advisory lock for
itself, so a worker that dies holds nothing.

Locks are taken in the order conversation, message, draft, and rows somebody else has
locked are left for the next run.
"""
import logging
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlmodel import Session

from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.core.db import get_session
from src.services.assistant.core.work import log_failure
from src.services.assistant.models.db import Conversation, Draft, Message

logger = logging.getLogger(__name__)

# What one run takes at most; the next one goes on.
MAX_RUN_SECONDS = 600.0
# Instead of the assistant's own 5 s: nobody waits for this.
STATEMENT_TIMEOUT = "60s"


class _LockTaken(Exception):
    """Another worker is at it."""


def run(stop: threading.Event) -> None:
    """One run. `stop` ends it after the current transaction."""
    started = time.monotonic()
    try:
        finished = _Run(stop, started + MAX_RUN_SECONDS).run()
    except _LockTaken:
        metrics.retention_lock_skipped_total.inc()
        logger.info("assistant: retention already runs in another worker")
        return
    except Exception as exc:
        metrics.retention_failures_total.inc()
        log_failure("retention", "assistant: retention failed", exc)
        return
    seconds = round(time.monotonic() - started, 1)
    if finished:
        metrics.retention_last_success_unixtime.set(time.time())
        logger.info("assistant: retention finished", extra={"seconds": seconds})
    elif not stop.is_set():
        logger.warning(
            "assistant: retention ran out of time", extra={"seconds": seconds}
        )


class _Run:
    def __init__(self, stop: threading.Event, deadline: float):
        self._stop = stop
        self._deadline = deadline
        self._batch = get_settings().retention_batch_size
        with get_session() as session:
            # One cutoff for the whole run, by the clock that stamped the rows.
            self._cutoff: datetime = session.scalar(
                sa.select(sa.func.now() - timedelta(days=get_settings().retention_days))
            )

    def run(self) -> bool:
        """Whether everything was looked at."""
        more = True
        while more:
            if self._must_end():
                return False
            more = self._delete_idle_conversations()
        last: uuid.UUID | None = None
        while True:
            if self._must_end():
                return False
            last = self._trim_conversations(after=last)
            if last is None:
                return True

    def _must_end(self) -> bool:
        return self._stop.is_set() or time.monotonic() > self._deadline

    def _begin(self, session: Session) -> None:
        taken = session.scalar(
            sa.select(
                sa.func.pg_try_advisory_xact_lock(get_settings().retention_lock_key)
            )
        )
        if not taken:
            raise _LockTaken()
        session.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))

    def _delete_idle_conversations(self) -> bool:
        """One batch; whether there may be more."""
        with get_session() as session, session.begin():
            self._begin(session)
            ids = session.scalars(
                sa.select(Conversation.id)
                .where(Conversation.updated_at < self._cutoff)
                .order_by(Conversation.updated_at)
                .limit(self._batch)
                .with_for_update(skip_locked=True)
            ).all()
            deleted = self._delete_conversations(session, ids)
        self._count(deleted)
        return len(ids) == self._batch

    def _trim_conversations(self, after: uuid.UUID | None) -> uuid.UUID | None:
        """One batch of conversations that are still in use, by id. The last id, or
        None when there are no more."""
        old_message = sa.exists().where(
            Message.conversation_id == Conversation.id,
            Message.role == "user",
            Message.created_at < self._cutoff,
        )
        with get_session() as session, session.begin():
            self._begin(session)
            query = sa.select(Conversation.id).where(
                Conversation.created_at < self._cutoff, old_message
            )
            if after is not None:
                query = query.where(Conversation.id > after)
            ids = session.scalars(
                query.order_by(Conversation.id)
                .limit(self._batch)
                .with_for_update(skip_locked=True)
            ).all()
            deleted = self._trim(session, ids)
        self._count(deleted)
        return ids[-1] if len(ids) == self._batch else None

    def _trim(self, session: Session, ids: Sequence[uuid.UUID]) -> dict[str, int]:
        # Cut at a user's message only: what answers it stays whole.
        first_kept = dict(
            session.execute(
                sa.select(Message.conversation_id, sa.func.min(Message.seq))
                .where(
                    Message.conversation_id.in_(ids),
                    Message.role == "user",
                    Message.created_at >= self._cutoff,
                )
                .group_by(Message.conversation_id)
            ).all()
        )
        deleted = self._delete_conversations(
            session, [id_ for id_ in ids if id_ not in first_kept]
        )
        for conversation_id, seq in first_kept.items():
            kept_from = session.scalar(
                sa.select(Message.created_at).where(
                    Message.conversation_id == conversation_id, Message.seq == seq
                )
            )
            deleted[Message.__tablename__] += session.execute(
                sa.delete(Message).where(
                    Message.conversation_id == conversation_id, Message.seq < seq
                )
            ).rowcount
            deleted[Draft.__tablename__] += session.execute(
                sa.delete(Draft).where(
                    Draft.conversation_id == conversation_id,
                    Draft.created_at < kept_from,
                )
            ).rowcount
        return deleted

    def _delete_conversations(
        self, session: Session, ids: Sequence[uuid.UUID]
    ) -> dict[str, int]:
        """Whole ones. Deleted rows by table."""
        deleted = {}
        for model, column in (
            (Message, Message.conversation_id),
            (Draft, Draft.conversation_id),
            (Conversation, Conversation.id),
        ):
            deleted[model.__tablename__] = (
                session.execute(sa.delete(model).where(column.in_(ids))).rowcount
                if ids
                else 0
            )
        return deleted

    def _count(self, deleted: dict[str, int]) -> None:
        """After the commit."""
        for table, rows in deleted.items():
            metrics.retention_rows_deleted_total.labels(table=table).inc(rows)
        if any(deleted.values()):
            logger.info("assistant: retention deleted rows", extra=deleted)
