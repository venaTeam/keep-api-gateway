"""Drafts whose Confirm never saw how its write ended: what the alerts look like now
decides. Nothing is written to alerts here.
"""
import asyncio
import logging
import time

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.core import metrics
from src.services.assistant.core.work import log_failure, run_db
from src.services.assistant.gateway.errors import GatewayError
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.kinds.base import DraftKind
from src.services.assistant.kinds.registry import kind_of
from src.services.assistant.models.api.conversation import ConversationView
from src.services.assistant.models.api.draft import DraftOutcome, DraftView
from src.services.assistant.models.identity import Identity

logger = logging.getLogger(__name__)

# Longer than any Confirm. Also the time between two attempts.
AFTER_SECONDS = 30
# The whole of it: somebody waits for the conversation.
READ_SECONDS = 4.0


def applying(view: ConversationView) -> list[DraftView]:
    return [
        item.draft
        for item in view.items
        if item.type == "draft" and item.draft.state == "applying"
    ]


async def reconcile(
    identity: Identity, drafts: list[DraftView], gateway: ReadGateway
) -> bool:
    """Give the drafts that are due their result. Whether any of them has one now.
    Reads only: nothing is written to alerts here."""
    settled = await asyncio.gather(*(_reconcile(identity, d, gateway) for d in drafts))
    return any(settled)


async def _reconcile(
    identity: Identity, draft: DraftView, gateway: ReadGateway
) -> bool:
    started = time.monotonic()
    kind = _readable(draft)
    if kind is None:
        return False
    try:
        key = drafts_bl.key_of(identity, draft)
        due = await run_db(drafts_bl.claim_look, key, AFTER_SECONDS)
        if due is None:
            return False
        # Under the draft's id, as its Confirm, within the time of one look.
        reader = gateway.for_request(due.id, deadline_seconds=READ_SECONDS)
        decision = await kind.reconcile(reader, due, None)
        if decision is None or decision[0] == "applying":
            return False  # not yet: the next look tries again
        state, outcome = decision
        settled = await run_db(
            drafts_bl.resolve,
            key,
            from_state="applying",
            state=state,
            outcome=outcome,
            event=outcomes.event_text(due, outcome),
            by_user=False,
        )
    except GatewayError:
        return False  # counted and logged by the client; the next look tries again
    except Exception as exc:
        log_failure("reconcile", "assistant: draft not reconciled", exc)
        return False
    if settled is None:
        return True  # by somebody else
    metrics.drafts_total.labels(kind=due.kind, event="finalized_after_timeout").inc()
    metrics.draft_outcomes_total.labels(kind=due.kind, outcome=outcome.code).inc()
    logger.info(
        "assistant: draft reconciled",
        extra={
            "tenant_id": key.tenant_id,
            "conversation_id": str(key.conversation_id),
            "draft_id": str(key.id),
            "state": state,
            "outcome": outcome.code,
            "seconds": round(time.monotonic() - started, 2),
        },
    )
    return True


def _readable(draft: DraftView) -> DraftKind | None:
    """None: a kind, a payload or a stored outcome this version cannot read, left by a
    newer one that may still be applying it. Such a draft is neither claimed nor
    closed here, and nothing is logged: it is looked at again on every view, and only
    the 24 h sweep closes it."""
    kind = kind_of(draft.kind)
    if kind is None:
        return None
    try:
        if draft.outcome is not None:
            DraftOutcome(**draft.outcome.dict())
        kind.payload_model.parse_obj(draft.payload)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return None
    return kind
