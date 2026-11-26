"""The chat stream: the one list of its events, their payloads and their framing.

keep-ui mirrors this in its types and SSE parser; tests/fixtures/
assistant_stream.json pins it on both sides.
"""
import json
from typing import Any, ClassVar, Dict, Literal, Optional, Tuple, Type, get_args
from uuid import UUID

from pydantic import BaseModel

from src.services.assistant.models.api.draft import DraftView

# Sent while nothing else is. An SSE comment, so it is not an event.
HEARTBEAT_FRAME = ": hb\n\n"

STREAM_MEDIA_TYPE = "text/event-stream"
STREAM_HEADERS = {
    # no-transform keeps Next's compression (which would buffer) off the stream.
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

ToolPhase = Literal["started", "finished"]
ToolOutcomeCode = Literal[
    "draft_created",
    "too_many_matches",
    "no_matches",
    "nothing_to_change",
    "invalid_cel",
    "invalid_arguments",
    "unauthenticated",
    "error",
]
StreamErrorCode = Literal[
    "llm_unavailable",
    "llm_rejected",
    "llm_rate_limited",
    "turn_timeout",
    "gateway_unavailable",
    "unauthenticated",
    "internal",
]
FinishReason = Literal["stop", "tool_limit", "length", "error", "cancelled"]


class StreamEvent(BaseModel):
    event: ClassVar[str]

    def data(self) -> Dict[str, Any]:
        return json.loads(self.json())

    def encode(self) -> str:
        # json.dumps escapes every newline, so the data is always one SSE line.
        return f"event: {self.event}\ndata: {json.dumps(self.data())}\n\n"


class ConversationEvent(StreamEvent):
    """Always first."""

    event: ClassVar[str] = "conversation"
    conversation_id: UUID
    user_message_id: UUID
    turn_id: UUID


class TextDeltaEvent(StreamEvent):
    """`message_id` is allocated per model round: a turn can have several."""

    event: ClassVar[str] = "text_delta"
    message_id: UUID
    delta: str


class ToolStatusEvent(StreamEvent):
    event: ClassVar[str] = "tool_status"
    tool_call_id: str
    tool: str
    phase: ToolPhase
    label: str
    # Only on `finished`; the key is absent otherwise.
    outcome: Optional[ToolOutcomeCode] = None

    def data(self) -> Dict[str, Any]:
        data = super().data()
        if data["outcome"] is None:
            del data["outcome"]
        return data


class DraftEvent(StreamEvent):
    """Sent after the draft row is committed."""

    event: ClassVar[str] = "draft"
    draft: DraftView


class ErrorEvent(StreamEvent):
    event: ClassVar[str] = "error"
    code: StreamErrorCode
    message: str
    retryable: bool


class DoneEvent(StreamEvent):
    """Always last. `message_id` is the last assistant message, if there is one."""

    event: ClassVar[str] = "done"
    message_id: Optional[UUID]
    finish_reason: FinishReason


STREAM_EVENTS: Tuple[Type[StreamEvent], ...] = (
    ConversationEvent,
    TextDeltaEvent,
    ToolStatusEvent,
    DraftEvent,
    ErrorEvent,
    DoneEvent,
)

# code -> (fixed message, retryable)
_STREAM_ERRORS: Dict[str, Tuple[str, bool]] = {
    "llm_unavailable": ("The assistant is unavailable right now. Try again.", True),
    "llm_rejected": (
        "The assistant could not continue. Start a new conversation.",
        False,
    ),
    "llm_rate_limited": ("The assistant is busy. Try again shortly.", True),
    "turn_timeout": ("The answer took too long. Try again.", True),
    "gateway_unavailable": ("Keep could not be reached. Try again.", True),
    "unauthenticated": ("Sign in again to use the assistant.", False),
    "internal": ("Something went wrong. Try again.", True),
}
assert set(_STREAM_ERRORS) == set(get_args(StreamErrorCode))


def stream_error(code: StreamErrorCode) -> ErrorEvent:
    message, retryable = _STREAM_ERRORS[code]
    return ErrorEvent(code=code, message=message, retryable=retryable)
