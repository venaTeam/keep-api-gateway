"""Confirm: the only code that writes, and only what the draft's card showed.

The gateway's own route decides whether the user may. Between the user's click and the
answer a draft is `applying`: nobody else can confirm it. Whatever happens before the
write puts it back to `pending`; after the write nothing does, because the write may
have changed something.

The draft's kind plans the writes and reads through a ReadGateway; this module alone
holds the client and sends them.
"""
import asyncio
import logging
import time
import uuid

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.core import metrics
from src.services.assistant.core.work import OngoingWork, log_failure, run_db
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.gateway.client import GatewayClient, WriteAnswer
from src.services.assistant.gateway.errors import (
    GatewayError,
    GatewayForbidden,
    GatewayUnauthorized,
    GatewayUnavailable,
)
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.kinds.base import (
    ConfirmContext,
    ConfirmPlan,
    Decision,
    DraftKind,
    Sent,
    Step,
    WriteRequest,
)
from src.services.assistant.kinds.registry import kind_of
from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.identity import Identity

logger = logging.getLogger(__name__)

# From the moment the request came in. Next's proxy gives up after 30 s of silence.
BUDGET_SECONDS = 26.0
# Either look, before and after the write.
READ_SECONDS = 4.0
# The write (12 s), the second look and the last transaction. A write that is sent with
# less than this could not be reported any more.
NEEDED_FOR_WRITE_SECONDS = 17.0
# The last transaction.
FINISH_SECONDS = 1.0
# A shutting-down worker stops waiting for the write in time to say so.
LEAVE_AT_SHUTDOWN_SECONDS = 1.5


async def confirm(
    identity: Identity,
    draft_id: uuid.UUID,
    gateway: GatewayClient,
    work: OngoingWork,
    started: float,
) -> DraftView:
    """Whoever calls this spawns it: it must not be cut off with the request. `started`
    is the time.monotonic() of the request. Raises AssistantError only.

    Whatever is refused before the claim leaves the draft as it is.
    """
    try:
        found = await run_db(drafts_bl.owned_draft, identity, draft_id)
        kind = kind_of(found.kind)
        if kind is None:
            # A newer version's: it may know how, this one does not.
            raise AssistantError("draft_unsupported")
        draft = await run_db(drafts_bl.claim, identity, draft_id)
        attempt = _Attempt(identity, draft, gateway, work, started, kind=kind)
        return await attempt.run()
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
        *,
        kind: DraftKind,
    ):
        self._key = drafts_bl.key_of(identity, draft)
        self._draft = draft
        self._kind = kind
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
        kind = self._kind
        plan = kind.plan(
            ConfirmContext(
                # Its looks take the place no chat call can.
                reader=ReadGateway(self._gateway, self._caller, critical=True),
                caller=self._caller,
                payload=kind.payload_model.parse_obj(self._draft.payload),
                confirm_input=None,
                read_seconds=self._read_seconds,
            )
        )
        step = await self._first(plan)
        sent: list[Sent] = []
        while isinstance(step, WriteRequest):
            if len(sent) >= len(plan.rows) or step.endpoint != plan.rows[len(sent)]:
                raise RuntimeError("a write the plan did not declare")
            answer = await self._write(step)
            sent.append(Sent(step, answer))
            if answer.result == "unknown":
                # The gateway may still be writing: a look now would show a half-way
                # state.
                return "applying", plan.pending(sent)
            step = await plan.after(sent)
        return step

    async def _first(self, plan: ConfirmPlan) -> Step:
        """The look before the write."""
        try:
            return await plan.first()
        except GatewayUnauthorized:
            raise AssistantError("unauthenticated") from None
        except GatewayForbidden:
            raise AssistantError("forbidden") from None
        except GatewayError:
            raise AssistantError("gateway_unavailable") from None

    async def _write(self, request: WriteRequest) -> WriteAnswer:
        send_task = asyncio.create_task(self._send(request))
        await self._work.wait(send_task, leave=LEAVE_AT_SHUTDOWN_SECONDS)
        if not send_task.done():
            send_task.cancel()  # ends the wait for it: the call itself goes on
            if self._sent:
                return WriteAnswer("unknown")
            raise AssistantError("not_ready")
        try:
            return send_task.result()
        except GatewayUnavailable:
            # It certainly never reached the gateway's route.
            self._sent = False
            raise AssistantError("gateway_unavailable") from None

    async def _send(self, request: WriteRequest) -> WriteAnswer:
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
        return await self._gateway.write(
            self._caller,
            request.endpoint,
            body=request.body,
            path_params=request.path_params,
            query=request.query,
        )

    def _read_seconds(self) -> float:
        """How long a look may take now: it must leave the last transaction its time."""
        return min(READ_SECONDS, self._seconds_left() - FINISH_SECONDS)

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
        # By type and id: a name is a user's text.
        saved = outcome.object
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
                "object_type": saved.type if saved is not None else None,
                "object_id": saved.id if saved is not None else None,
                "seconds": round(time.monotonic() - self._started, 2),
            },
        )
