"""Confirm: the only code that changes alerts, and only those the user's card listed.

The gateway's own route decides whether the user may. Between the user's click and the
answer a draft is `applying`: nobody else can confirm it. Whatever happens before the
write puts it back to `pending`; after the write nothing does, because the alerts may
have been changed.
"""
import asyncio
import logging
import time
import uuid
from typing import Any

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.bl.outcomes import Decision
from src.services.assistant.core import metrics
from src.services.assistant.core.work import OngoingWork, log_failure, run_db
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.gateway.client import FetchResult, GatewayClient
from src.services.assistant.gateway.errors import (
    GatewayError,
    GatewayForbidden,
    GatewayUnauthorized,
    GatewayUnavailable,
)
from src.services.assistant.models.api.draft import (
    PAYLOAD_MODELS,
    ChangeAlertStatusPayload,
    DraftView,
    GatewayResult,
    TargetStatus,
)
from src.services.assistant.models.identity import Identity

logger = logging.getLogger(__name__)

# From the moment the request came in. Next's proxy gives up after 30 s of silence.
BUDGET_SECONDS = 26.0
# Either look at the alerts, before and after the write.
READ_SECONDS = 4.0
# The write (12 s), the second look and the last transaction. A write that is sent with
# less than this could not be reported any more.
NEEDED_FOR_WRITE_SECONDS = 17.0
# The last transaction.
FINISH_SECONDS = 1.0
# A shutting-down worker stops waiting for the write in time to say so.
LEAVE_AT_SHUTDOWN_SECONDS = 1.5


def status_change(
    fingerprints: list[str],
    status: TargetStatus,
    note: str | None,
    dispose_on_new_alert: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The body and query of the `batch_enrich` write: what the UI sends for the same
    change."""
    enrichments: dict[str, Any] = {"status": status}
    if status != "suppressed":
        enrichments["dismiss_mode"] = None
        enrichments["dismissed_until"] = None
    if note:
        enrichments["note"] = note
    return (
        {"fingerprints": fingerprints, "enrichments": enrichments},
        {"dispose_on_new_alert": "true" if dispose_on_new_alert else "false"},
    )


async def confirm(
    identity: Identity,
    draft_id: uuid.UUID,
    gateway: GatewayClient,
    work: OngoingWork,
    started: float,
) -> DraftView:
    """Whoever calls this spawns it: it must not be cut off with the request. `started`
    is the time.monotonic() of the request. Raises AssistantError only."""
    try:
        draft = await run_db(drafts_bl.claim, identity, draft_id)
        return await _Attempt(identity, draft, gateway, work, started).run()
    except AssistantError:
        raise
    except Exception as exc:
        log_failure("confirm", "assistant: confirm failed", exc)
        raise AssistantError("internal") from None


class _Attempt:
    def __init__(
        self,
        identity: Identity,
        draft: DraftView,
        gateway: GatewayClient,
        work: OngoingWork,
        started: float,
    ):
        self._key = drafts_bl.key_of(identity, draft)
        self._draft = draft
        # The audit trail of the gateway carries the draft's id.
        self._caller = identity.caller(draft.id)
        self._gateway = gateway
        self._work = work
        self._started = started
        self._sent = False

    async def run(self) -> DraftView:
        try:
            decision = await self._decide()
        except BaseException:
            if not self._sent:
                await self._revert()
            raise
        state, outcome = decision
        if state == "applying":
            draft = await run_db(drafts_bl.keep_applying, self._key, outcome)
        else:
            draft = await run_db(
                drafts_bl.resolve,
                self._key,
                from_state="applying",
                state=state,
                outcome=outcome,
                event=outcomes.event_text(self._draft, outcome),
            )
        if draft is None:
            # A look at the conversation has settled it meanwhile.
            draft = await run_db(drafts_bl.load, self._key)
        self._report(draft, decision)
        return draft

    async def _decide(self) -> Decision:
        payload: ChangeAlertStatusPayload = PAYLOAD_MODELS[self._draft.kind].parse_obj(
            self._draft.payload
        )
        settled = outcomes.before_write(payload, await self._look_before(payload))
        to_write = [
            alert.fingerprint
            for alert in payload.alerts
            if alert.fingerprint not in settled
        ]
        if not to_write:
            return outcomes.nothing_to_change(payload, settled)
        result = await self._write(payload, to_write)
        if result in outcomes.REFUSED:
            return outcomes.refused(payload, settled, result)
        # The gateway may still be writing: a look now would show a half-way state.
        now = None if result == "unknown" else await self._look_after(to_write)
        if now is not None:
            return outcomes.verified(payload, settled, result, now)
        if result == "ok":
            return outcomes.unverified(payload, settled)
        return outcomes.pending(payload, settled, result)

    async def _look_before(self, payload: ChangeAlertStatusPayload) -> FetchResult:
        try:
            return await self._gateway.fetch_alerts_by_fingerprints(
                self._caller,
                [alert.fingerprint for alert in payload.alerts],
                deadline_seconds=READ_SECONDS,
            )
        except GatewayUnauthorized:
            raise AssistantError("unauthenticated") from None
        except GatewayForbidden:
            raise AssistantError("forbidden") from None
        except GatewayError:
            raise AssistantError("gateway_unavailable") from None

    async def _write(
        self, payload: ChangeAlertStatusPayload, fingerprints: list[str]
    ) -> GatewayResult:
        send_task = asyncio.create_task(self._send(payload, fingerprints))
        await self._work.wait(send_task, leave=LEAVE_AT_SHUTDOWN_SECONDS)
        if not send_task.done():
            send_task.cancel()  # ends the wait for it: the call itself goes on
            if self._sent:
                return "unknown"
            raise AssistantError("not_ready")
        try:
            return send_task.result()
        except GatewayUnavailable:
            # It certainly never reached the gateway's route.
            self._sent = False
            raise AssistantError("gateway_unavailable") from None

    async def _send(
        self, payload: ChangeAlertStatusPayload, fingerprints: list[str]
    ) -> GatewayResult:
        body, query = status_change(
            fingerprints,
            payload.target_status,
            payload.note,
            payload.dispose_on_new_alert,
        )
        # No await between these checks and the call.
        if self._work.draining:
            raise AssistantError("not_ready")
        if self._seconds_left() < NEEDED_FOR_WRITE_SECONDS:
            logger.warning(
                "assistant: confirm too slow to write",
                extra={"draft_id": str(self._draft.id)},
            )
            raise AssistantError("gateway_unavailable")
        self._sent = True
        answer = await self._gateway.write(
            self._caller, "batch_enrich", body=body, query=query
        )
        return answer.result

    async def _look_after(self, fingerprints: list[str]) -> FetchResult | None:
        """What the alerts look like now; None when that cannot be had in time."""
        seconds = min(READ_SECONDS, self._seconds_left() - FINISH_SECONDS)
        if seconds <= 0:
            return None
        try:
            return await self._gateway.fetch_alerts_by_fingerprints(
                self._caller, fingerprints, deadline_seconds=seconds
            )
        except GatewayError:
            return None  # counted and logged by the client

    def _seconds_left(self) -> float:
        left = self._started + BUDGET_SECONDS - time.monotonic()
        return min(left, self._work.grace_left())

    async def _revert(self) -> None:
        try:
            await run_db(drafts_bl.revert, self._key)
        except Exception as exc:
            # It stays `applying` until somebody looks at the conversation.
            log_failure("confirm_revert", "assistant: draft not put back", exc)

    def _report(self, draft: DraftView, decision: Decision) -> None:
        _, outcome = decision
        metrics.drafts_total.labels(kind=draft.kind, event="confirmed").inc()
        metrics.draft_outcomes_total.labels(
            kind=draft.kind, outcome=outcome.code
        ).inc()
        logger.info(
            "assistant: draft confirmed",
            extra={
                "tenant_id": self._key.tenant_id,
                "conversation_id": str(draft.conversation_id),
                "draft_id": str(draft.id),
                "state": draft.state,
                "outcome": outcome.code,
                "gateway_result": outcome.gateway_result,
                "applied": outcome.applied_count,
                "failed": outcome.failed_count,
                "unconfirmed": outcome.unconfirmed_count,
                "seconds": round(time.monotonic() - self._started, 2),
            },
        )
