"""What every tool provides. A tool only reads and proposes: it has no write path.

A new tool is one module with a ToolSpec, plus its line in the registry. A tool that
makes drafts names its kind of `kinds/registry.py` in `draft_kind`; one that links to
a page says so in `makes_link`. The chat turn counts both against their caps.
"""
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any

from pydantic import BaseModel

from src.services.assistant.bl.projection import clean_text
from src.services.assistant.gateway.errors import (
    GatewayForbidden,
    GatewayUnauthorized,
    GatewayUnavailable,
    InvalidCel,
    QueryFailed,
)
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.llm.client import LlmTool
from src.services.assistant.models.api.stream import StreamErrorCode, ToolOutcomeCode
from src.services.assistant.models.identity import Identity

# Units of a turn's filter budget: a filter the model wrote costs at least one.
MAX_FILTER_LOOKUPS_PER_TURN = 6
# Links a turn makes; past them, a link tool gets the tool error `link_limit`.
MAX_LINKS_PER_TURN = 3
MAX_TOOL_RESULT_CHARS = 8000
MAX_DIAGNOSTICS_SHOWN = 5
MAX_DIAGNOSTIC_CHARS = 300


def _this_second() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass
class ToolContext:
    """One turn's view of the world, shared by all its tool calls."""

    identity: Identity
    # Reads only, under the turn's id; `for_request` gives one under another.
    gateway: ReadGateway
    # Model-written filters have no early exit on the gateway's side. Charged through
    # `take_filter_lookup` only.
    cel_lookups_left: int = MAX_FILTER_LOOKUPS_PER_TURN
    # The turn's instant, the one its context message shows: UTC, whole seconds.
    now: datetime = field(default_factory=_this_second)
    # The user's time zone, for times they give in their own words. datetime's own UTC
    # when their browser named none this server has data for: ZoneInfo("UTC") would
    # need that data too. `zone_name` is its IANA name, then "UTC".
    zone: tzinfo = timezone.utc
    zone_name: str = "UTC"

    def take_filter_lookup(self, units: int = 1) -> bool:
        """Charge `units` of the turn's filter budget. True when that many were left;
        False, with nothing charged, when not."""
        if units < 1:
            raise ValueError("a lookup costs at least one unit")
        if units > self.cel_lookups_left:
            return False
        self.cel_lookups_left -= units
        return True


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
    # The kind of the drafts it makes, None for a tool that makes none: the per-turn
    # draft cap and the kill switch apply to it.
    draft_kind: str | None = None
    # It links to a page: the per-turn link cap applies to it.
    makes_link: bool = False

    def llm_tool(self) -> LlmTool:
        return LlmTool(self.name, self.description, self.parameters)


def tool_error(code: str, *, stream_error: StreamErrorCode | None = None):
    return ToolOutcome(
        "error", {"status": "error", "code": code}, stream_error=stream_error
    )


# What a tool's read can fail with that it tells the model, never raises:
# `except READ_FAILURES as exc: return read_failure_outcome(exc)`.
READ_FAILURES: tuple[type[Exception], ...] = (
    InvalidCel,
    QueryFailed,
    GatewayUnauthorized,
    GatewayForbidden,
    GatewayUnavailable,
)


def read_failure_outcome(exc: Exception) -> ToolOutcome:
    """The outcome of a read that failed with one of READ_FAILURES."""
    if isinstance(exc, InvalidCel):
        return ToolOutcome(
            "invalid_cel",
            {
                "status": "invalid_cel",
                "diagnostics": [
                    clean_text(str(item.get("message", "")), MAX_DIAGNOSTIC_CHARS)
                    for item in exc.diagnostics[:MAX_DIAGNOSTICS_SHOWN]
                ],
            },
        )
    if isinstance(exc, QueryFailed):
        # The expression itself can do this; the model can ask for another wording.
        return tool_error("query_failed")
    if isinstance(exc, GatewayUnauthorized):
        return ToolOutcome(
            "unauthenticated",
            {"status": "unauthenticated"},
            stream_error="unauthenticated",
        )
    if isinstance(exc, GatewayForbidden):
        return tool_error("forbidden")
    if isinstance(exc, GatewayUnavailable):
        return tool_error("gateway_unavailable", stream_error="gateway_unavailable")
    raise TypeError(f"not a read failure: {type(exc).__name__}") from exc


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
