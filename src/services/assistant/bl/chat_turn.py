"""One chat turn: the loop between the model and the tools, and its stream.

Two tasks. The producer talks to the model, runs tools and stores rows. The supervisor
waits for the producer, the deadline or a stop, ends the producer, stores what is left
and releases the turn lock. Neither belongs to the HTTP response: a client that goes
away only asks for a stop. Nothing here has a write path to alerts.
"""
import asyncio
import functools
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field

from src.services.assistant.api.route import describe_exception
from src.services.assistant.bl import context as context_bl
from src.services.assistant.bl import conversations_bl, history, prompt
from src.services.assistant.bl.conversations_bl import NewMessage, TurnLost, TurnRef
from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.core.work import OngoingWork, log_failure, run_db
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.llm.client import (
    Finished,
    LlmClient,
    LlmError,
    TextDelta,
    ToolCallComplete,
    Usage,
    make_safety_identifier,
)
from src.services.assistant.models.api.chat import ChatRequest
from src.services.assistant.models.api.stream import (
    HEARTBEAT_FRAME,
    ConversationEvent,
    DoneEvent,
    DraftEvent,
    FinishReason,
    StreamErrorCode,
    StreamEvent,
    TextDeltaEvent,
    ToolStatusEvent,
    stream_error,
)
from src.services.assistant.models.identity import Identity
from src.services.assistant.tools.base import (
    ToolContext,
    ToolOutcome,
    encode_result,
    tool_error,
)
from src.services.assistant.tools.registry import TOOLS

logger = logging.getLogger(__name__)

# A producer that does not end when it is cancelled is left behind: without the turn
# lock it cannot store anything.
PRODUCER_END_SECONDS = 10.0


class TurnFailed(Exception):
    """The turn cannot go on; `code` is what the user is told."""

    def __init__(self, code: StreamErrorCode):
        super().__init__(code)
        self.code = code


@dataclass
class _Round:
    """One completion of the model."""

    message_id: uuid.UUID = field(default_factory=uuid.uuid4)
    text: str = ""
    calls: list[ToolCallComplete] = field(default_factory=list)
    usage: Usage | None = None
    reason: str = ""
    # Its row is committed.
    saved: bool = False


@dataclass(frozen=True)
class _End:
    """How a turn ended."""

    finish: FinishReason
    # The metric label.
    result: str
    # Of an answer that was cut short.
    status: str = "error"
    error: StreamErrorCode | None = None


_STOPPED = _End("cancelled", "cancelled", status="interrupted")
_TIMED_OUT = _End("error", "timeout", error="turn_timeout")


def _failed(code: StreamErrorCode) -> _End:
    return _End("error", "error", error=code)


class Turn:
    def __init__(
        self,
        ref: TurnRef,
        identity: Identity,
        request: ChatRequest,
        gateway: ReadGateway,
        llm: LlmClient,
    ):
        self.ref = ref
        self._identity = identity
        self._chat_request = request
        # Reads only: a turn never writes.
        self._gateway = gateway.for_request(ref.turn_id)
        self._llm = llm
        self._tools = ToolContext(identity=identity, gateway=self._gateway)
        self._round = _Round()
        self._rounds = 0
        self._drafts = 0
        # The last stored answer that has text.
        self._message_id: uuid.UUID | None = None
        self._stop = asyncio.Event()
        self._frames: asyncio.Queue[str | None] = asyncio.Queue()
        self._supervisor: asyncio.Task[None] | None = None
        self._closed: asyncio.Task[None] | None = None

    def start(self, work: OngoingWork) -> None:
        self._emit(
            ConversationEvent(
                conversation_id=self.ref.conversation_id,
                user_message_id=self.ref.user_message_id,
                turn_id=self.ref.turn_id,
            )
        )
        self._supervisor = work.spawn(self._supervise(), stoppable=self)
        self._closed = work.spawn(self._close(work, self._supervisor))

    def stop(self) -> None:
        """End the turn as interrupted, unless it has ended already."""
        self._stop.set()

    async def closed(self) -> None:
        """Until the lock is released and everything is stored, or the worker has no
        more time for it. Cancelling this ends the wait only."""
        if self._closed is not None:
            await asyncio.wait({self._closed})

    async def _close(self, work: OngoingWork, supervisor: asyncio.Task[None]) -> None:
        """Ends the response, in a worker that shuts down at the latest when its grace
        is over: a slow database must not take the rest of the worker's time."""
        try:
            await work.wait(supervisor, leave=0.0)
        finally:
            self._frames.put_nowait(None)

    async def frames(self) -> AsyncGenerator[str, None]:
        heartbeat = get_settings().heartbeat_seconds
        while True:
            try:
                frame = await asyncio.wait_for(self._frames.get(), heartbeat)
            except TimeoutError:
                yield HEARTBEAT_FRAME
                continue
            if frame is None:
                return
            yield frame

    def _emit(self, event: StreamEvent) -> None:
        self._frames.put_nowait(event.encode())

    # --- the end -----------------------------------------------------------------

    async def _supervise(self) -> None:
        started = time.monotonic()
        end = await self._run()
        await self._release(end)
        # Only now: the next message must not find the lock taken.
        if end.error is not None:
            self._emit(stream_error(end.error))
        self._emit(DoneEvent(message_id=self._message_id, finish_reason=end.finish))
        self._report(end, time.monotonic() - started)

    async def _run(self) -> _End:
        producer = asyncio.create_task(self._converse())
        stopped = asyncio.create_task(self._stop.wait())
        await asyncio.wait(
            {producer, stopped},
            timeout=get_settings().turn_deadline_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if producer.done():
            end = self._end_of(producer)
        else:
            end = _STOPPED if stopped.done() else _TIMED_OUT
            producer.cancel()
            await asyncio.wait({producer}, timeout=PRODUCER_END_SECONDS)
            if not producer.done():
                metrics.internal_errors_total.labels(site="turn_producer").inc()
                logger.error("assistant: a turn's producer does not end")
            elif not producer.cancelled():
                producer.exception()  # retrieved: asyncio would log its text
        stopped.cancel()
        return end

    def _end_of(self, producer: asyncio.Task[FinishReason]) -> _End:
        if producer.cancelled():
            return _STOPPED
        exc = producer.exception()
        if exc is None:
            finish = producer.result()
            return _End(finish, "tool_limit" if finish == "tool_limit" else "ok")
        if isinstance(exc, TurnFailed):
            return _failed(exc.code)
        if isinstance(exc, LlmError):
            return _failed(exc.code)
        if isinstance(exc, TurnLost):
            logger.warning(
                "assistant: a turn lost its conversation",
                extra={"turn_id": str(self.ref.turn_id)},
            )
        else:
            log_failure("turn", "assistant: turn failed", exc)
        return _failed("internal")

    async def _release(self, end: _End) -> None:
        partial = None
        round_ = self._round
        if round_.text and not round_.saved:
            round_.calls = []  # nothing will answer them
            partial = self._answer(round_, status=end.status)
        for rest in [None] if partial is None else [partial, None]:
            try:
                kept = await run_db(conversations_bl.end_turn, self.ref, rest)
            except Exception as exc:
                # Without its text then. The lock also ends by itself.
                log_failure("turn_end", "assistant: turn not closed", exc)
                continue
            if kept and rest is not None:
                self._message_id = rest.id
            return

    def _report(self, end: _End, seconds: float) -> None:
        metrics.turns_total.labels(result=end.result).inc()
        metrics.turn_duration_seconds.observe(seconds)
        logger.info(
            "assistant: turn finished",
            extra={
                "tenant_id": self.ref.tenant_id,
                "conversation_id": str(self.ref.conversation_id),
                "turn_id": str(self.ref.turn_id),
                "result": end.result,
                "finish_reason": end.finish,
                "stream_error": end.error,
                "rounds": self._rounds,
                "drafts": self._drafts,
                "seconds": round(seconds, 2),
            },
        )

    # --- the conversation ------------------------------------------------------------

    async def _converse(self) -> FinishReason:
        settings = get_settings()
        context, earlier = await asyncio.gather(
            context_bl.gather(
                self._identity, self._gateway, self._chat_request.context, self.ref.turn_id
            ),
            run_db(history.load, self.ref),
        )
        messages = [
            prompt.system_message(TOOLS.values()),
            *earlier,
            prompt.context_message(context),
            {"role": "user", "content": self._chat_request.message},
        ]
        while True:
            out_of_rounds = self._rounds >= settings.max_tool_rounds
            no_tools = out_of_rounds or self._drafts >= settings.max_drafts_per_turn
            round_ = await self._complete(messages, "none" if no_tools else "auto")
            if no_tools:
                round_.calls = []  # an endpoint may not keep to it
            await self._save(round_)
            if not round_.calls:
                if out_of_rounds:
                    return "tool_limit"
                return "length" if round_.reason == "length" else "stop"
            self._rounds += 1
            messages.append(
                history.assistant_message(
                    round_.text,
                    [
                        history.tool_call(c.id, c.name, c.arguments)
                        for c in round_.calls
                    ],
                )
            )
            for call in round_.calls:
                result = await self._call_tool(call)
                messages.append(history.tool_reply(call.id, result))

    async def _complete(self, messages: list[dict], tool_choice) -> _Round:
        self._round = round_ = _Round()
        clean = conversations_bl.storable
        async with aclosing(
            self._llm.stream(
                messages,
                [tool.llm_tool() for tool in TOOLS.values()],
                tool_choice=tool_choice,
                safety_identifier=make_safety_identifier(
                    self._identity.tenant_id, self._identity.email
                ),
            )
        ) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    text = clean(event.text)
                    if text:
                        round_.text += text
                        self._emit(
                            TextDeltaEvent(message_id=round_.message_id, delta=text)
                        )
                elif isinstance(event, ToolCallComplete):
                    round_.calls.append(
                        ToolCallComplete(
                            clean(event.id), clean(event.name), clean(event.arguments)
                        )
                    )
                elif isinstance(event, Usage):
                    round_.usage = event
                    self._count_tokens(event)
                elif isinstance(event, Finished):
                    round_.reason = event.reason
        return round_

    def _count_tokens(self, usage: Usage) -> None:
        for type_, tokens in (
            ("prompt", usage.prompt_tokens),
            ("completion", usage.completion_tokens),
        ):
            metrics.llm_tokens_total.labels(
                tenant_id=self._identity.tenant_id, model=self._llm.model, type=type_
            ).inc(tokens)

    def _answer(self, round_: _Round, status: str = "complete") -> NewMessage:
        return NewMessage(
            "assistant",
            round_.text,
            id=round_.message_id,
            status=status,
            tool_calls=[
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in round_.calls
            ]
            or None,
            model=self._llm.model,
            prompt_tokens=round_.usage and round_.usage.prompt_tokens,
            completion_tokens=round_.usage and round_.usage.completion_tokens,
        )

    async def _save(self, round_: _Round) -> None:
        if not round_.text and not round_.calls:
            return
        answer = self._answer(round_)

        def save() -> None:
            conversations_bl.add_messages(self.ref, [answer])
            # In the thread: a cancelled wait must not lose it.
            round_.saved = True
            if round_.text:
                self._message_id = round_.message_id

        await run_db(save)

    # --- tools -----------------------------------------------------------------------

    async def _call_tool(self, call: ToolCallComplete) -> str:
        """Run it, store its result and tell the user. The result, for the model."""
        tool = TOOLS.get(call.name)
        status = functools.partial(
            ToolStatusEvent,
            tool_call_id=call.id,
            tool=call.name,
            label=tool.label if tool else "",
        )
        if tool is not None:
            self._emit(status(phase="started"))
        outcome = await self._outcome(call)
        metrics.tool_calls_total.labels(
            tool=call.name if tool else "unknown", outcome=outcome.code
        ).inc()

        result = encode_result(outcome.result)
        reply = NewMessage(
            "tool",
            result,
            id=uuid.uuid4(),
            tool_call_id=call.id,
            draft_id=outcome.draft.id if outcome.draft else None,
        )
        view = await run_db(
            conversations_bl.add_messages, self.ref, [reply], draft=outcome.draft
        )
        if view is not None:
            self._drafts += 1
            metrics.drafts_total.labels(kind=view.kind, event="created").inc()
            self._emit(DraftEvent(draft=view))
        if tool is not None:
            self._emit(status(phase="finished", outcome=outcome.code))
        if outcome.stream_error is not None:
            raise TurnFailed(outcome.stream_error)
        return result

    async def _outcome(self, call: ToolCallComplete) -> ToolOutcome:
        tool = TOOLS.get(call.name)
        if tool is None:
            return tool_error("unknown_tool")
        # Several calls can come in one round.
        if self._drafts >= get_settings().max_drafts_per_turn:
            return tool_error("draft_limit")
        return await tool.run(self._tools, call.arguments)


async def begin(
    identity: Identity,
    request: ChatRequest,
    conversation_id: uuid.UUID | None,
    gateway: ReadGateway,
    llm: LlmClient,
    work: OngoingWork,
) -> Turn:
    """Take the turn lock and start. Whoever calls this spawns it: a request that goes
    away in the middle must not leave the lock behind."""
    ref = await run_db(
        conversations_bl.start_turn, identity, conversation_id, request.message
    )
    try:
        if work.draining:
            raise AssistantError("not_ready")
        turn = Turn(ref, identity, request, gateway, llm)
        turn.start(work)
        return turn
    except BaseException as exc:
        try:
            await run_db(conversations_bl.end_turn, ref)
        except Exception as end_exc:
            log_failure("turn_end", "assistant: turn not closed", end_exc)
        if not isinstance(exc, (AssistantError, asyncio.CancelledError)):
            logger.error("assistant: turn not started", extra=describe_exception(exc))
        raise
