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
]
AlertResult = Literal["applied", "failed", "unconfirmed", "skipped", "not_found"]


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


class AlertOutcome(BaseModel):
    fingerprint: str
    name: Optional[str]
    result: AlertResult
    status_after: Optional[str]


class DraftOutcome(BaseModel):
    code: OutcomeCode
    message: str
    # None when nothing was sent to the gateway.
    gateway_result: Optional[GatewayResult]
    applied_count: int
    failed_count: int
    unconfirmed_count: int
    skipped_count: int
    not_found_count: int
    results: List[AlertOutcome]
    # None while the outcome is still pending.
    finished_at: Optional[datetime]


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
    outcome: Optional[DraftOutcome]


# kind -> payload model, one entry per registered tool.
PAYLOAD_MODELS = {"change_alert_status": ChangeAlertStatusPayload}
