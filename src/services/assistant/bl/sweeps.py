"""What becomes of drafts nobody comes back to. Deliberately across tenants.

Blocking: runs in a thread. Every step can run in several workers at once: each change
is one conditional UPDATE, as everywhere.
"""
import logging
import threading
from collections import Counter
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.bl.drafts_bl import DraftKey
from src.services.assistant.core import metrics
from src.services.assistant.core.db import get_session
from src.services.assistant.core.work import log_failure
from src.services.assistant.kinds.base import Decision
from src.services.assistant.kinds.registry import kind_of
from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.db import Draft

logger = logging.getLogger(__name__)

# Longer than any Confirm, and than a user keeps looking at one.
STALE_AFTER = timedelta(minutes=5)
# Nobody has looked at the conversation since.
GIVE_UP_AFTER = timedelta(hours=24)

_BATCH = 200


def run(stop: threading.Event) -> None:
    """One run. `stop` ends it after the current step."""
    for site, step in (
        ("sweep_expire", _expire),
        ("sweep_stale", _report_stale),
        ("sweep_close", _close_abandoned),
    ):
        if stop.is_set():
            return
        try:
            step(stop)
        except Exception as exc:  # the other steps still run
            log_failure(site, "assistant: draft sweep failed", exc)


def _expire(stop: threading.Event) -> None:
    while not stop.is_set():
        with get_session() as session, session.begin():
            due = session.scalars(
                sa.select(Draft.id)
                .where(Draft.state == "pending", Draft.expires_at < sa.func.now())
                .limit(_BATCH)
                .with_for_update(skip_locked=True)
            ).all()
            kinds = session.scalars(
                sa.update(Draft)
                .where(Draft.id.in_(due))
                .values(
                    state="expired",
                    resolved_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
                .returning(Draft.kind)
                .execution_options(synchronize_session=False)
            ).all()
        for kind, count in Counter(kinds).items():
            metrics.drafts_total.labels(kind=kind, event="expired").inc(count)
        if len(kinds) < _BATCH:
            return


def _report_stale(stop: threading.Event) -> None:
    """Drafts whose result is not known and that nobody looks at."""
    with get_session() as session:
        stale = session.scalar(
            sa.select(sa.func.count())
            .select_from(Draft)
            .where(
                Draft.state == "applying",
                Draft.updated_at < sa.func.now() - STALE_AFTER,
                Draft.updated_at >= sa.func.now() - GIVE_UP_AFTER,
            )
        )
    metrics.drafts_stale_applying.set(stale)
    if stale:
        logger.warning("assistant: drafts stuck in applying", extra={"drafts": stale})


def _close_abandoned(stop: threading.Event) -> None:
    """What happened to them cannot be told any more: the alerts have moved on."""
    with get_session() as session:
        abandoned = session.execute(
            sa.select(
                Draft.id,
                Draft.tenant_id,
                Draft.created_by,
                Draft.conversation_id,
                Draft.kind,
            )
            .where(
                Draft.state == "applying",
                Draft.updated_at < sa.func.now() - GIVE_UP_AFTER,
            )
            .order_by(Draft.updated_at)
            .limit(_BATCH)
        ).all()
    for row in abandoned:
        if stop.is_set():
            return
        try:
            _close(row)
        except Exception as exc:  # one of them must not keep the others
            log_failure("sweep_close", "assistant: draft not closed", exc)


def _close(row: Any) -> None:
    key = DraftKey(row.id, row.tenant_id, row.created_by, row.conversation_id)
    draft = drafts_bl.load(key)
    state, outcome = _decide(draft)
    closed = drafts_bl.resolve(
        key,
        from_state="applying",
        state=state,
        outcome=outcome,
        event=outcomes.event_text(draft, outcome),
        by_user=False,
        # A newer version's keys stay, for whoever looks into it later.
        merge_outcome=True,
    )
    if closed is None:
        return  # by another worker
    metrics.draft_outcomes_total.labels(kind=draft.kind, outcome=outcome.code).inc()
    logger.warning(
        "assistant: draft closed without a result",
        extra={"tenant_id": key.tenant_id, "draft_id": str(key.id)},
    )


def _decide(draft: DraftView) -> Decision:
    """By the kind; generically when this version cannot read the kind, the payload or
    the stored outcome (left behind by a newer version), so such rows never stay."""
    kind = kind_of(draft.kind)
    if kind is None:
        return outcomes.unreadable_for_good()
    try:
        payload = kind.payload_model.parse_obj(draft.payload)
        return kind.close_unknown(payload, draft.outcome, None)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return outcomes.unreadable_for_good()
