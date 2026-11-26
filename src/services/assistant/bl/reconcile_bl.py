"""Drafts whose Confirm never saw how its write ended: what the alerts look like now
decides. Nothing is written to alerts here.
"""
import asyncio
import logging
import time

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.core import metrics
from src.services.assistant.core.work import log_failure, run_db
from src.services.assistant.gateway.client import GatewayClient
from src.services.assistant.gateway.errors import GatewayError
from src.services.assistant.models.api.conversation import ConversationView
from src.services.assistant.models.api.draft import (
    PAYLOAD_MODELS,
    ChangeAlertStatusPayload,
    DraftOutcome,
    DraftView,
)
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
    identity: Identity, drafts: list[DraftView], gateway: GatewayClient
) -> bool:
    """Give the drafts that are due their result. Whether any of them has one now."""
    settled = await asyncio.gather(*(_reconcile(identity, d, gateway) for d in drafts))
    return any(settled)


async def _reconcile(
    identity: Identity, draft: DraftView, gateway: GatewayClient
) -> bool:
    started = time.monotonic()
    payload = _readable(draft)
    if payload is None:
        return False
    try:
        key = drafts_bl.key_of(identity, draft)
        due = await run_db(drafts_bl.claim_look, key, AFTER_SECONDS)
        if due is None:
            return False
        before_write = outcomes.settled_before_write(due.outcome)
        now = await gateway.fetch_alerts_by_fingerprints(
            identity.caller(due.id),
            [
                alert.fingerprint
                for alert in payload.alerts
                if alert.fingerprint not in before_write
            ],
            deadline_seconds=READ_SECONDS,
        )
        state, outcome = outcomes.verified(
            payload,
            before_write,
            # Without an outcome the worker ended in the middle of it.
            due.outcome.gateway_result if due.outcome else "unknown",
            now,
            after_timeout=True,
        )
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


def _readable(draft: DraftView) -> ChangeAlertStatusPayload | None:
    """None: a kind, a payload or a stored outcome this version cannot read, left by a
    newer one that may still be applying it. Such a draft is neither claimed nor
    closed here, and nothing is logged: it is looked at again on every view, and only
    the 24 h sweep closes it."""
    model = PAYLOAD_MODELS.get(draft.kind)
    if model is None:
        return None
    try:
        if draft.outcome is not None:
            DraftOutcome(**draft.outcome.dict())
        return model.parse_obj(draft.payload)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return None
