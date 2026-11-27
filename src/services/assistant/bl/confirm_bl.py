"""Confirm: the only code that writes, and only what the draft's card showed.

The gateway's own route decides whether the user may. Between the user's click and the
answer a draft is `applying`: nobody else can confirm it. Whatever happens before the
write puts it back to `pending`; after the write nothing does, because the write may
have changed something.

The draft's kind plans the writes and reads through a ReadGateway; this module alone
holds the client and sends them. A write is sent only with the time left to send every
write after it and tell the result (`seconds_needed`).
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Optional

from pydantic import BaseModel

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.core import metrics
from src.services.assistant.core.work import OngoingWork, log_failure, run_db
from src.services.assistant.exceptions import AssistantError, validation_error
from src.services.assistant.gateway.client import (
    LATE_ANSWER_SECONDS,
    GatewayClient,
    WriteAnswer,
    write_timeout,
)
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
from src.services.assistant.models.api.confirm import ConfirmRequest
from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.identity import Identity

logger = logging.getLogger(__name__)

# From the moment the request came in. Next's proxy gives up after 30 s of silence.
BUDGET_SECONDS = 26.0
# A look: before the first write, and after the last one at most.
READ_SECONDS = 4.0
# The transaction after each write: the checkpoint before the next one, or the last.
FINISH_SECONDS = 1.0
# A shutting-down worker stops waiting for the write in time to say so.
LEAVE_AT_SHUTDOWN_SECONDS = 1.5

# The clock of `started` and of every time check. Tests give their own.
monotonic: Callable[[], float] = time.monotonic


def seconds_needed(timeouts: Sequence[float], k: int, read_after: float) -> float:
    """The time that must be left to send write `k` (from 0) of a plan whose writes
    may take `timeouts` (in the order of its rows): that write and every one after it,
    each with the wait for a late answer and the transaction after it, then the look
    after the last one, which may take `read_after`. A write sent with less could not
    be reported any more.

    sum over j = k..n-1 of (timeout_j + LATE) + (read_after + LATE) + FINISH * (n - k)
    """
    left = timeouts[k:]
    return (
        sum(timeout + LATE_ANSWER_SECONDS for timeout in left)
        + (read_after + LATE_ANSWER_SECONDS)
        + FINISH_SECONDS * len(left)
    )


async def confirm(
    identity: Identity,
    draft_id: uuid.UUID,
    gateway: GatewayClient,
    work: OngoingWork,
    started: float,
    body: Any = None,
) -> DraftView:
    """Whoever calls this spawns it: it must not be cut off with the request. `started`
    is the time.monotonic() of the request; `body` the request's JSON, None without.
    Raises AssistantError only.

    Whatever is refused before the claim leaves the draft as it is. The body is checked
    once the draft was found: another user's draft does not exist for any body.
    """
    try:
        found = await run_db(drafts_bl.owned_draft, identity, draft_id)
        kind = kind_of(found.kind)
        if kind is None:
            # A newer version's: it may know how, this one does not.
            raise AssistantError("draft_unsupported")
        confirm_input = _confirm_input(kind, body)
        draft = await run_db(drafts_bl.claim, identity, draft_id)
        attempt = _Attempt(
            identity,
            draft,
            gateway,
            work,
            started,
            kind=kind,
            confirm_input=confirm_input,
        )
        return await attempt.run()
    except AssistantError:
        raise
    except Exception as exc:
        log_failure("confirm", "assistant: confirm failed", exc)
        raise AssistantError("internal") from None


def _confirm_input(kind: DraftKind, body: Any) -> BaseModel | None:
    """The body, checked against what the kind takes. Raises a 400."""
    if body is not None and not isinstance(body, dict):
        raise validation_error("body", "invalid_value")
    try:
        derived = ConfirmRequest.parse_obj(body or {}).derived
    except ValueError:  # pydantic's ValidationError is a ValueError
        raise validation_error("derived", "invalid_value") from None
    model = kind.confirm_input_model
    if model is None:
        if derived is not None:
            raise validation_error("derived", "invalid_value")
        return None
    if derived is None:
        # A keep-ui that does not know the kind's form yet.
        raise validation_error("derived", "field_required")
    try:
        return model.parse_obj(derived)
    except ValueError:
        raise validation_error("derived", "invalid_value") from None


class _NotSent(Exception):
    """A write that was not sent: nothing of it reached the gateway. `code` is the
    error a Confirm answers with when it was the first."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
        confirm_input: BaseModel | None = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._key = drafts_bl.key_of(identity, draft)
        self._draft = draft
        self._kind = kind
        self._confirm_input = confirm_input
        # The audit trail of the gateway carries the draft's id.
        self._caller = identity.caller(draft.id)
        self._gateway = gateway
        self._work = work
        self._started = started
        self._clock = clock or monotonic
        # Writes that got past their checks: each may have reached the gateway.
        self._writes_sent = 0
        # The timeouts of the plan's rows, in their order.
        self._timeouts: tuple[float, ...] = ()
        # How long the next look may take: READ_SECONDS, or what the last write sent
        # asked for the look after it.
        self._read_limit = READ_SECONDS

    @property
    def _sent(self) -> bool:
        """Whether anything may have been written: then nothing puts the draft back."""
        return self._writes_sent > 0

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
                confirm_input=self._confirm_input,
                read_seconds=self._read_seconds,
            )
        )
        # Raises for a row the client does not have: before anything is read or sent.
        self._timeouts = tuple(write_timeout(row) for row in plan.rows)
        step = await self._first(plan)
        sent: list[Sent] = []
        while isinstance(step, WriteRequest):
            k = len(sent)
            self._check(plan, k, step)
            answer = await self._write(step, k)
            if answer is None:
                # A later write: what the earlier ones did stands.
                return plan.stopped(sent)
            sent.append(Sent(step, answer))
            self._read_limit = step.read_after_seconds
            if answer.result == "unknown":
                # The gateway may still be writing: a look now would show a half-way
                # state.
                return "applying", plan.pending(sent)
            step = await plan.after(sent)
        return step

    @staticmethod
    def _check(plan: ConfirmPlan, k: int, step: WriteRequest) -> None:
        """Write `k` is the plan's own. A plan that asks for another is a bug, and
        nothing of it is sent."""
        if k >= len(plan.rows) or step.endpoint != plan.rows[k]:
            raise RuntimeError("a write the plan did not declare")
        if not 0 <= step.read_after_seconds <= READ_SECONDS:
            raise RuntimeError("a look after the write longer than Confirm's")

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

    async def _write(self, request: WriteRequest, k: int) -> WriteAnswer | None:
        """The answer to write `k`. When it was not sent: None for a later write, and
        for the first an AssistantError, since nothing was written at all."""
        last = k == len(self._timeouts) - 1
        # Before a write that is not the last, the look after the last one is not
        # known yet: it may take READ_SECONDS.
        read_after = request.read_after_seconds if last else READ_SECONDS
        needed = seconds_needed(self._timeouts, k, read_after)
        send_task = asyncio.create_task(self._send(request, k, needed))
        await self._work.wait(send_task, leave=LEAVE_AT_SHUTDOWN_SECONDS)
        if not send_task.done():
            send_task.cancel()  # ends the wait for it: the call itself goes on
            if self._writes_sent > k:
                return WriteAnswer("unknown")
            code = "not_ready"
        else:
            try:
                return send_task.result()
            except _NotSent as refused:
                code = refused.code
        if k == 0:
            raise AssistantError(code)
        return None

    async def _send(self, request: WriteRequest, k: int, needed: float) -> WriteAnswer:
        # No await between these checks and the call.
        if self._work.draining:
            raise _NotSent("not_ready")
        if self._seconds_left() < needed:
            metrics.confirm_budget_refusals_total.labels(
                kind=self._kind.name, step=str(k)
            ).inc()
            logger.warning(
                "assistant: confirm too slow to write",
                extra={"draft_id": str(self._draft.id), "step": k},
            )
            raise _NotSent("gateway_unavailable")
        self._writes_sent = k + 1
        try:
            return await self._gateway.write(
                self._caller,
                request.endpoint,
                body=request.body,
                path_params=request.path_params,
                query=request.query,
            )
        except GatewayUnavailable:
            # It certainly never reached the gateway's route.
            self._writes_sent = k
            raise _NotSent("gateway_unavailable") from None

    def _read_seconds(self) -> float:
        """How long a look may take now: it must leave the last transaction its time."""
        return min(self._read_limit, self._seconds_left() - FINISH_SECONDS)

    def _seconds_left(self) -> float:
        left = self._started + BUDGET_SECONDS - self._clock()
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
                "seconds": round(self._clock() - self._started, 2),
            },
        )
