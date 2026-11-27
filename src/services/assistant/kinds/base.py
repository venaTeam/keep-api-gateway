"""What every draft kind provides. A kind decides what its Confirm writes and how to
tell what became of it; it never writes itself.

A kind builds its writes as data (`WriteRequest`) and reads through a `ReadGateway`.
Confirm (`bl/confirm_bl.py`) is the one orchestrator: it sends each write, through the
allowlisted `GatewayClient.write`, and stores what the plan decides.

A new kind is one module with a `DraftKind`, plus its line in `kinds/registry.py`.
"""
import abc
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Optional, Union

from pydantic import BaseModel

from src.services.assistant.gateway.client import EMPTY, Caller, WriteAnswer
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.models.api.draft import (
    DraftOutcome,
    DraftState,
    DraftView,
    StoredOutcome,
)

# (final state, outcome) of a draft; "applying" is not final.
Decision = tuple[DraftState, DraftOutcome]

# The look after a write, unless the request asks for less: confirm_bl.READ_SECONDS.
READ_AFTER_SECONDS = 4.0

# `confirm_state`: Confirm's own record in the draft's stored outcome, given to
# `reconcile` and `close_unknown` (None when there is none). Confirm stores it at the
# checkpoint before each write, and again when it fails after one; it stays until the
# draft is settled:
#   "step": the write (from 0) that may have been sent last;
#   "sent_at": ISO 8601 in UTC: that write was not sent before this moment;
#   "object_ids": per write that came back when it was stored, in order, the id of
#     what it made, or None (none, or not known). Before write k it has k of them;
#     after a failure, k + 1 when write k came back.
# Server-made values only, never a model's or a user's text; never shown to anyone.
MAX_CONFIRM_STATE_BYTES = 4096


@dataclass(frozen=True)
class WriteRequest:
    """One write a plan asks Confirm to send. Data only: the client checks it against
    its row before anything is sent."""

    # A row of the client's write table.
    endpoint: str
    body: Union[dict[str, Any], list[Any]]
    # Each value a canonical UUID; a positive integer for the row's int_path_params.
    path_params: Mapping[str, str] = EMPTY
    # Keys and values from the row's allowlist.
    query: Mapping[str, str] = EMPTY
    # How long the look after this write may take: 0..READ_AFTER_SECONDS.
    read_after_seconds: float = READ_AFTER_SECONDS


@dataclass(frozen=True)
class Sent:
    """A write that was sent, and what came back. `answer.result` is "unknown" when
    nobody could wait for the answer."""

    request: WriteRequest
    answer: WriteAnswer


# What a plan asks for next: a write, or the end.
Step = Union[WriteRequest, Decision]


@dataclass(frozen=True)
class ConfirmContext:
    """What a kind gets for one Confirm."""

    # Reads only, on behalf of `caller`.
    reader: ReadGateway
    # On whose behalf; its request id is the draft's.
    caller: Caller
    # The draft's payload, parsed with the kind's `payload_model`.
    payload: BaseModel
    # The body of the Confirm request, parsed with `confirm_input_model`; None without.
    confirm_input: Optional[BaseModel]
    # How long a look may take now: Confirm's own budget decides.
    read_seconds: Callable[[], float]


@dataclass(frozen=True)
class ConfirmPlan:
    """What one Confirm of a draft does. Confirm calls `first`, sends the write it
    returns, then calls `after` with every write sent so far, until a Decision."""

    # Every endpoint the plan may send, in order: write k must be `rows[k]`.
    rows: tuple[str, ...]
    # The look before the first write. A GatewayError here puts the draft back.
    first: Callable[[], Awaitable[Step]]
    # The last of `sent` was answered: the next write, or the end.
    after: Callable[[Sequence[Sent]], Awaitable[Step]]
    # The last answer is not known: what to store while the draft stays applying.
    pending: Callable[[Sequence[Sent]], DraftOutcome]
    # A later write could not be sent (time, shutdown): what is known so far, the
    # objects made among it. Its outcome is also what the checkpoint before a later
    # write stores while the draft stays applying, so it is called after any answered
    # write that another row follows.
    stopped: Callable[[Sequence[Sent]], Decision]


class DraftKind(abc.ABC):
    """One kind of draft. Its name is the name of the tool that makes it."""

    name: ClassVar[str]
    payload_model: ClassVar[type[BaseModel]]
    # The body a Confirm of this kind needs (its `derived`); None: it takes none.
    confirm_input_model: ClassVar[Optional[type[BaseModel]]] = None
    # Whether keep-ui can open a draft of this kind in its own form instead; such a
    # draft is put away with the reason `opened_in_form`.
    can_open_in_form: ClassVar[bool] = False
    # Every scope one of its writes needs. A hint for the card and the context only:
    # the gateway's own routes decide.
    scope_hint: ClassVar[frozenset[str]]

    @abc.abstractmethod
    def write_scope(self, payload: BaseModel) -> str:
        """The scope this payload's writes need (a hint, as above)."""

    @abc.abstractmethod
    def plan(self, ctx: ConfirmContext) -> ConfirmPlan:
        """The writes of one Confirm. Nothing is read or sent until Confirm asks."""

    @abc.abstractmethod
    async def reconcile(
        self,
        reader: ReadGateway,
        view: DraftView,
        confirm_state: Optional[Mapping[str, Any]],
    ) -> Optional[Decision]:
        """A draft whose Confirm never saw how its write ended, by what can be read now
        (on behalf of the draft's owner, within the time of one look). A final
        Decision, or None to try again later."""

    @abc.abstractmethod
    def close_unknown(
        self,
        payload: BaseModel,
        stored: Optional[StoredOutcome],
        confirm_state: Optional[Mapping[str, Any]],
    ) -> Decision:
        """Nobody has looked for 24 hours: the final Decision without a look.
        Synchronous and pure: no reads, no database."""

    @abc.abstractmethod
    def event_text(self, draft: DraftView, outcome: DraftOutcome) -> str:
        """What the model is told of a result. Ids, codes and counts only: no text that
        came from an alert or a user, since it is read as the system's own words.
        Synchronous and pure; `draft.payload` may not be readable."""

    @abc.abstractmethod
    def discarded_text(self, draft: DraftView, reason: Optional[str]) -> str:
        """What the model is told when the user put the draft away. As `event_text`.
        `reason`: why, as the discard request said (None: not said)."""
