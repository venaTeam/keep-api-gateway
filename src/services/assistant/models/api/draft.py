"""Draft shapes shared by the API, the stream and the stored JSON columns.

keep-ui mirrors these types; tests/fixtures/assistant_stream.json pins them.
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel

# Alerts in one draft. A product decision, so not an environment variable.
MAX_ALERTS_PER_DRAFT = 20

DraftState = Literal[
    "pending",
    "applying",
    "applied",
    "partially_applied",
    "failed",
    "discarded",
    "expired",
]
TargetStatus = Literal["firing", "acknowledged", "resolved", "suppressed"]
OutcomeCode = Literal[
    "applied",
    "partially_applied",
    "nothing_to_change",
    "unverified",
    "outcome_pending",
    "not_permitted",
    "read_only",
    "failed",
    "interrupted",
    "outcome_unknown",
    # Of drafts that save a preset, a correlation rule or a dashboard.
    "name_taken",
    "object_gone",
    "stale_draft",
    "invalid_filter",
    "opened_in_form",
    "rules_unreadable",
]
GatewayResult = Literal[
    "ok",
    "failed_response",
    "rejected",
    "rejected_maybe_written",
    "http_error",
    "read_only",
    "not_permitted",
    "unauthenticated",
    "unknown",
    "not_found",
]
AlertResult = Literal["applied", "failed", "unconfirmed", "skipped", "not_found"]
# What a draft can save, other than alerts.
ObjectType = Literal["preset", "correlation_rule", "dashboard"]


class FingerprintSelector(BaseModel):
    type: Literal["fingerprints"] = "fingerprints"
    fingerprints: List[str]


class CelSelector(BaseModel):
    type: Literal["cel"] = "cel"
    cel: str


class DraftAlert(BaseModel):
    """One alert the draft would change. Only `fingerprint` is guaranteed."""

    fingerprint: str
    name: Optional[str]
    severity: Optional[str]
    from_status: Optional[str]
    service: Optional[str]
    source: Optional[str]
    last_received: Optional[str]
    # None: this read could not tell (the single-alert endpoint omits incidents).
    has_incident: Optional[bool]
    has_note: bool


class SkippedAlert(BaseModel):
    """Matched, but already in the target status."""

    fingerprint: str
    name: Optional[str]
    status: Optional[str]


class ChangeAlertStatusPayload(BaseModel):
    version: Literal[1] = 1
    selector: Union[FingerprintSelector, CelSelector]
    target_status: TargetStatus
    note: Optional[str]
    dispose_on_new_alert: bool
    # Alerts the selector matched: to change plus skipped.
    matched_count: int
    alerts: List[DraftAlert]
    skipped: List[SkippedAlert]
    not_found: List[str]
    # Requested fingerprints the gateway offers no safe way to look up.
    unresolvable: List[str]


class StoredAlertOutcome(BaseModel):
    """As read back: a newer version may have stored a result this one does not know."""

    fingerprint: str
    name: Optional[str]
    result: str
    status_after: Optional[str]


class AlertOutcome(StoredAlertOutcome):
    result: AlertResult


class DraftObject(BaseModel):
    """What the write made or changed. Logged by type and id only: the name is a
    user's text."""

    type: ObjectType
    id: str
    # None when this version could not tell.
    name: Optional[str]
    # The page in keep-ui, for a link; None when there is none to show.
    path: Optional[str]


class StoredOutcome(BaseModel):
    """As read back. Strict on write, lenient on load: a rollback leaves outcomes of a
    newer version behind, with values and keys this one does not know."""

    code: str
    message: str
    # None when nothing was sent to the gateway.
    gateway_result: Optional[str]
    applied_count: int
    failed_count: int
    unconfirmed_count: int
    skipped_count: int
    not_found_count: int
    results: List[StoredAlertOutcome]
    # None while the outcome is still pending.
    finished_at: Optional[datetime]
    # None for a draft of alerts, and while nothing was made or changed.
    object: Optional[DraftObject]


class DraftOutcome(StoredOutcome):
    """What this version writes."""

    code: OutcomeCode
    gateway_result: Optional[GatewayResult]
    results: List[AlertOutcome]


class DraftView(BaseModel):
    id: UUID
    conversation_id: UUID
    # The tool name; an open set, so the payload type depends on it.
    kind: str
    state: DraftState
    permitted: bool
    created_at: datetime
    expires_at: datetime
    resolved_at: Optional[datetime]
    payload: Dict[str, Any]
    outcome: Optional[StoredOutcome]
