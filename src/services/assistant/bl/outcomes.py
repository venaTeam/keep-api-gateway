"""Outcomes of any kind of draft: how a stored one is read back, the close of a draft
this version cannot read, and what the model is told. Pure: no reads, no writes.

What one kind's outcome holds is the kind's business (`kinds/`).
"""
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Optional, get_args

from src.services.assistant.kinds.alert_set import counts_text
from src.services.assistant.kinds.base import Decision
from src.services.assistant.kinds.registry import kind_of
from src.services.assistant.models.api.draft import (
    AlertResult,
    DraftOutcome,
    DraftView,
    GatewayResult,
    ObjectType,
    OutcomeCode,
    StoredObject,
    StoredOutcome,
)

# Of an outcome a newer version stored, with a value this one does not know.
UNREADABLE_OUTCOME = (
    "This version of Keep Assistant cannot show the result of this change."
)
_OUTCOME_UNKNOWN = "The result of this change could not be determined."


def readable(stored: Mapping) -> StoredOutcome:
    """A stored outcome as the card shows it. Unknown keys are dropped; an unknown
    value, an object's type among them, keeps its place, and the text becomes one that
    claims nothing. An object without the shape of one is left out, with the same
    text."""
    object_parsed = _object_parsed(stored.get("object"))
    if not object_parsed:
        stored = {key: value for key, value in stored.items() if key != "object"}
    outcome = StoredOutcome.parse_obj(stored)
    known = (
        object_parsed
        and outcome.code in get_args(OutcomeCode)
        and (
            outcome.gateway_result is None
            or outcome.gateway_result in get_args(GatewayResult)
        )
        and all(item.result in get_args(AlertResult) for item in outcome.results)
        and (outcome.object is None or outcome.object.type in get_args(ObjectType))
    )
    return outcome if known else outcome.copy(update={"message": UNREADABLE_OUTCOME})


def fully_readable(outcome: StoredOutcome) -> bool:
    """Whether `readable` knew every value of it and left nothing out. Only these are
    acted on (reconcile, the sweep): of another, what the newer version did is not
    known. No outcome is stored with the text `readable` gives the others."""
    return outcome.message != UNREADABLE_OUTCOME


def _object_parsed(stored: object) -> bool:
    if stored is None:
        return True
    try:
        StoredObject.parse_obj(stored)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return False
    return True


def unreadable_for_good() -> Decision:
    """The 24 h close of a draft whose kind, payload or stored outcome this version
    cannot read: nothing about what it did is claimed."""
    return "failed", DraftOutcome(
        code="outcome_unknown",
        message=_OUTCOME_UNKNOWN,
        gateway_result="unknown",
        applied_count=0,
        failed_count=0,
        unconfirmed_count=0,
        skipped_count=0,
        not_found_count=0,
        results=[],
        finished_at=datetime.now(timezone.utc),
    )


def event_text(draft: DraftView, outcome: DraftOutcome) -> str:
    """What the model is told: by the draft's kind, or generically for one this version
    does not know. No text that came from an alert or from a user: it is read as the
    system's own words."""
    kind = kind_of(draft.kind)
    if kind is not None:
        return kind.event_text(draft, outcome)
    return f"{_subject(draft)}: {outcome.code}. {counts_text(outcome)}"


def discarded_text(draft: DraftView, reason: Optional[str] = None) -> str:
    kind = kind_of(draft.kind)
    if kind is not None:
        return kind.discarded_text(draft, reason)
    return f"{_subject(draft)}: discarded by the user."


def _subject(draft: DraftView) -> str:
    # Of a kind this version does not know, nothing in the payload is understood.
    return f"draft {draft.id} ({draft.kind})"
