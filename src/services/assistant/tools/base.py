"""What every tool provides. A tool only reads and proposes: it has no write path.

A new tool is one module with a ToolSpec, plus its line in the registry. A tool that
makes drafts also has a kind of the same name in `kinds/registry.py`.
"""
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.llm.client import LlmTool
from src.services.assistant.models.api.stream import StreamErrorCode, ToolOutcomeCode
from src.services.assistant.models.identity import Identity

MAX_CEL_LOOKUPS_PER_TURN = 4
MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class ToolContext:
    """One turn's view of the world, shared by all its tool calls."""

    identity: Identity
    # Reads only, under the turn's id; `for_request` gives one under another.
    gateway: ReadGateway
    # Model-written filters have no early exit on the gateway's side.
    cel_lookups_left: int = MAX_CEL_LOOKUPS_PER_TURN


@dataclass(frozen=True)
class NewDraft:
    """A draft to store. The caller commits it before anyone hears of it."""

    id: uuid.UUID
    kind: str
    permitted: bool
    payload: BaseModel


@dataclass(frozen=True)
class ToolOutcome:
    code: ToolOutcomeCode
    # For the model. Text from alerts in it is already cleaned.
    result: dict[str, Any]
    draft: NewDraft | None = None
    # Set when the turn cannot sensibly go on.
    stream_error: StreamErrorCode | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    # Shown to the user while the tool runs.
    label: str
    # Appended to the system prompt.
    prompt_guidance: str
    # (context, raw JSON arguments). Raises only on a bug.
    run: Callable[[ToolContext, str], Awaitable[ToolOutcome]]

    def llm_tool(self) -> LlmTool:
        return LlmTool(self.name, self.description, self.parameters)


def tool_error(code: str, *, stream_error: StreamErrorCode | None = None):
    return ToolOutcome(
        "error", {"status": "error", "code": code}, stream_error=stream_error
    )


def encode_result(result: dict[str, Any]) -> str:
    """Compact JSON within the budget of one tool message: the lists are cut to
    fewer items until it fits. The card, not the model, shows them in full."""
    encoded = json.dumps(result, separators=(",", ":"))
    for keep in (10, 5, 2, 0):
        if len(encoded) <= MAX_TOOL_RESULT_CHARS:
            break
        cut = {k: v[:keep] if isinstance(v, list) else v for k, v in result.items()}
        encoded = json.dumps({**cut, "truncated": True}, separators=(",", ":"))
    return encoded
