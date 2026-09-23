import datetime
import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    Extra,
    Field,
    PrivateAttr,
    root_validator,
    validator,
)
from sqlmodel import col, desc

from src.models.alert import INCIDENT_STATUS_OWNED_KEYS
from src.models.db.incident import (
    Incident,
    IncidentDismissMode,
    IncidentSeverity,
    IncidentStatus,
)
from src.models.db.rule import ResolveOn, Rule


class IncidentStatusChangeDto(BaseModel):
    status: IncidentStatus
    comment: str | None
    dispose_on_new_alert: bool = False
    tagged_users: list[str] = []

    # Only meaningful when status is SUPPRESSED. `dismiss_mode` defaults to
    # permanent; `dismissed_until` is required for, and only for, a time-boxed
    # dismissal.
    dismiss_mode: IncidentDismissMode | None = None
    dismissed_until: datetime.datetime | None = None

    @validator("tagged_users")
    @classmethod
    def validate_no_duplicate_users(cls, value):
        """Ensure there are no duplicate users in the tagged_users list."""
        if len(value) != len(set(value)):
            unique_users = list(
                dict.fromkeys(value)
            )  # Preserves order while removing duplicates
            return unique_users
        return value

    @root_validator
    def validate_dismiss_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Reject dismiss payloads that would persist a state no reader can make
        sense of, rather than silently dropping or half-applying them."""
        status = values.get("status")
        dismiss_mode = values.get("dismiss_mode")
        dismissed_until = values.get("dismissed_until")

        if status != IncidentStatus.SUPPRESSED:
            if dismiss_mode is not None or dismissed_until is not None:
                raise ValueError(
                    "dismiss_mode/dismissed_until are only valid with "
                    f"status='{IncidentStatus.SUPPRESSED.value}'"
                )
            return values

        # Suppressing without saying how means "permanently".
        if dismiss_mode is None:
            dismiss_mode = IncidentDismissMode.PERMANENT
            values["dismiss_mode"] = dismiss_mode

        if dismiss_mode == IncidentDismissMode.DISMISS_UNTIL:
            if dismissed_until is None:
                raise ValueError(
                    "dismissed_until is required when "
                    f"dismiss_mode='{IncidentDismissMode.DISMISS_UNTIL.value}'"
                )
            # Naive input is read as UTC, matching the timezone-aware column.
            if dismissed_until.tzinfo is None:
                dismissed_until = dismissed_until.replace(
                    tzinfo=datetime.timezone.utc
                )
                values["dismissed_until"] = dismissed_until
            # A deadline in the past would store a dismissal that reads as
            # already expired — the caller almost certainly meant something else.
            if dismissed_until <= datetime.datetime.now(datetime.timezone.utc):
                raise ValueError("dismissed_until must be in the future")
        else:
            # A permanent dismissal carries no deadline.
            values["dismissed_until"] = None

        return values


def split_incident_status_keys(
    enrichments: dict,
) -> tuple[Optional[IncidentStatusChangeDto], dict]:
    """Split an incident enrich payload into (status change, real enrichments).

    The UI sends a dismissal and its note as one body. Status and dismiss keys
    belong on the incident's typed columns, everything else in the enrichment
    JSONB, so they are separated here and applied in one transaction by
    `IncidentBl.enrich_and_change_status`.

    Returns `(None, enrichments)` when the payload carries no status keys. The
    status half is validated by `IncidentStatusChangeDto`, so a bad dismissal is
    rejected before either half is written — the same rules the dedicated status
    endpoint enforces, rather than a second looser copy of them.
    """
    rest = {
        key: value
        for key, value in enrichments.items()
        if key not in INCIDENT_STATUS_OWNED_KEYS
    }
    status_keys = {
        key: value
        for key, value in enrichments.items()
        if key in INCIDENT_STATUS_OWNED_KEYS
    }

    if not status_keys:
        return None, rest

    if "status" not in status_keys:
        # dismiss_mode/dismissed_until on their own are meaningless: dismissal IS
        # the suppressed status. Rather than guess, say so.
        raise ValueError(
            "dismiss_mode/dismissed_until require an accompanying "
            f"status='{IncidentStatus.SUPPRESSED.value}'"
        )

    return (
        IncidentStatusChangeDto(
            status=status_keys["status"],
            comment=None,
            dismiss_mode=status_keys.get("dismiss_mode"),
            dismissed_until=status_keys.get("dismissed_until"),
        ),
        rest,
    )


class IncidentSeverityChangeDto(BaseModel):
    severity: IncidentSeverity
    comment: str | None


class IncidentDtoIn(BaseModel):
    user_generated_name: str | None
    assignee: str | None
    user_summary: str | None
    same_incident_in_the_past_id: UUID | None
    severity: IncidentSeverity | None

    class Config:
        extra = Extra.allow
        schema_extra = {
            "examples": [
                {
                    "id": "c2509cb3-6168-4347-b83b-a41da9df2d5b",
                    "name": "Incident name",
                    "user_summary": "Keep: Incident description",
                    "status": "firing",
                }
            ]
        }


class IncidentDto(IncidentDtoIn):
    id: UUID

    start_time: datetime.datetime | None
    last_seen_time: datetime.datetime | None
    end_time: datetime.datetime | None
    creation_time: datetime.datetime | None

    alerts_count: int
    alert_sources: list[str]
    status: IncidentStatus = IncidentStatus.FIRING
    # Dismiss state behind a SUPPRESSED status. `dismissed_until` stays populated
    # after it lapses (status reverts on its own) so the UI can show when a
    # dismissal ended, not just that it did.
    dismiss_mode: IncidentDismissMode | None = None
    dismissed_until: datetime.datetime | None = None
    assignee: str | None
    services: list[str]

    is_predicted: bool
    is_candidate: bool

    generated_summary: str | None
    ai_generated_name: str | None

    rule_fingerprint: str | None
    fingerprint: (
        str | None
    )  # This is the fingerprint of the incident generated by the underlying tool

    same_incident_in_the_past_id: UUID | None

    merged_into_incident_id: UUID | None
    merged_by: str | None
    merged_at: datetime.datetime | None

    enrichments: dict | None = {}
    incident_type: str | None
    incident_application: str | None

    resolve_on: str = Field(
        default=ResolveOn.ALL.value,
        description="Resolution strategy for the incident",
    )

    rule_id: UUID | None
    rule_name: str | None
    rule_is_deleted: bool | None

    _tenant_id: str = PrivateAttr()
    # AlertDto, not explicitly typed because of circular dependency
    _alerts: Optional[List] = PrivateAttr(default=None)

    def __init__(self, **data):
        super().__init__(**data)
        if "alerts" in data:
            self._alerts = data["alerts"]
        if "tenant_id" in data:
            self._tenant_id = data.pop("tenant_id")

    def __str__(self) -> str:
        # Convert the model instance to a dictionary
        model_dict = self.dict()
        return json.dumps(model_dict, indent=4, default=str)

    class Config:
        extra = Extra.allow
        schema_extra = IncidentDtoIn.Config.schema_extra
        underscore_attrs_are_private = True

        json_encoders = {
            # Converts UUID to their values for JSON serialization
            UUID: lambda v: str(v),
        }

    @property
    def name(self):
        return self.user_generated_name or self.ai_generated_name

    @property
    def alerts(self) -> List:
        if self._alerts is not None:
            return self._alerts

        from repositories.db import get_incident_alerts_by_incident_id
        from utils.enrichment_helpers import convert_db_alerts_to_dto_alerts

        try:
            if not self._tenant_id:
                return []
        except Exception:
            logging.getLogger(__name__).error(
                "Tenant ID is not set in incident",
                extra={"incident_id": self.id},
            )
            return []
        alerts, _ = get_incident_alerts_by_incident_id(self._tenant_id, str(self.id))
        return convert_db_alerts_to_dto_alerts(alerts)

    @root_validator(pre=True)
    def set_default_values(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Check and set default status
        status = values.get("status")
        try:
            values["status"] = IncidentStatus(status)
        except ValueError:
            logging.getLogger(__name__).warning(
                f"Invalid status value: {status}, setting default.",
                extra={"event": values},
            )
            values["status"] = IncidentStatus.FIRING
        return values

    @classmethod
    def from_db_incident(cls, db_incident: "Incident", rule: "Rule" = None):
        severity = (
            IncidentSeverity.from_number(db_incident.severity)
            if isinstance(db_incident.severity, int)
            else db_incident.severity
        )

        # some default value for resolve_on
        if not db_incident.resolve_on:
            db_incident.resolve_on = ResolveOn.ALL.value

        dto = cls(
            id=db_incident.id,
            user_generated_name=db_incident.user_generated_name,
            ai_generated_name=db_incident.ai_generated_name,
            user_summary=db_incident.user_summary,
            generated_summary=db_incident.generated_summary,
            is_predicted=db_incident.is_predicted,
            is_candidate=db_incident.is_candidate,
            creation_time=db_incident.creation_time,
            start_time=db_incident.start_time,
            last_seen_time=db_incident.last_seen_time,
            end_time=db_incident.end_time,
            alerts_count=db_incident.alerts_count,
            alert_sources=db_incident.sources or [],
            severity=severity,
            status=db_incident.status,
            dismiss_mode=db_incident.dismiss_mode,
            dismissed_until=db_incident.dismissed_until,
            assignee=db_incident.assignee,
            services=db_incident.affected_services or [],
            rule_fingerprint=db_incident.rule_fingerprint,
            fingerprint=db_incident.fingerprint,
            same_incident_in_the_past_id=db_incident.same_incident_in_the_past_id,
            merged_into_incident_id=db_incident.merged_into_incident_id,
            merged_by=db_incident.merged_by,
            merged_at=db_incident.merged_at,
            incident_type=db_incident.incident_type,
            incident_application=str(db_incident.incident_application),
            enrichments=db_incident.enrichments,
            resolve_on=db_incident.resolve_on,
            rule_id=rule.id if rule else None,
            rule_name=rule.name if rule else None,
            rule_is_deleted=rule.is_deleted if rule else None,
        )

        # This field is required for getting alerts when required
        dto._tenant_id = db_incident.tenant_id

        if db_incident.enrichments:
            # Status and dismiss state are owned by the typed columns, so a
            # `status`/`dismiss_*` key in the JSONB — pre-migration rows still
            # carry them — must not overlay what was read above. The enrich route
            # refuses to write them going forward
            # (INCIDENT_STATUS_OWNED_KEYS); this drops the ones already stored.
            overlay = {
                key: value
                for key, value in db_incident.enrichments.items()
                if key not in INCIDENT_STATUS_OWNED_KEYS
            }
            if overlay:
                dto = dto.copy(update=overlay)

        # Derived last so it wins outright. Same precedence as the COALESCE chain
        # CEL filters compile to, so the list view and this DTO can't disagree
        # about what is suppressed.
        if db_incident.is_dismiss_active():
            dto.status = IncidentStatus.SUPPRESSED

        return dto

    def to_db_incident(self) -> "Incident":
        """Converts an IncidentDto instance to an Incident database model."""
        from src.models.db.alert import Incident

        # `suppressed` is derived, never stored — persisting it would leave the
        # incident stuck suppressed once the dismissal lapsed, since nothing
        # sweeps the table. Record it as the dismiss state it actually is and
        # keep FIRING as the status it reverts to.
        status = self.status
        dismiss_mode = self.dismiss_mode
        dismissed_until = self.dismissed_until
        if status == IncidentStatus.SUPPRESSED:
            status = IncidentStatus.FIRING
            if dismiss_mode is None:
                dismiss_mode = IncidentDismissMode.PERMANENT
                dismissed_until = None

        db_incident = Incident(
            id=self.id,
            user_generated_name=self.user_generated_name,
            ai_generated_name=self.ai_generated_name,
            user_summary=self.user_summary,
            generated_summary=self.generated_summary,
            assignee=self.assignee,
            severity=self.severity.order,
            status=status.value,
            dismiss_mode=dismiss_mode.value if dismiss_mode else None,
            dismissed_until=dismissed_until,
            creation_time=self.creation_time or datetime.datetime.utcnow(),
            start_time=self.start_time,
            end_time=self.end_time,
            last_seen_time=self.last_seen_time,
            alerts_count=self.alerts_count,
            affected_services=self.services,
            sources=self.alert_sources,
            is_predicted=self.is_predicted,
            is_candidate=self.is_candidate,
            rule_fingerprint=self.rule_fingerprint,
            fingerprint=self.fingerprint,
            same_incident_in_the_past_id=self.same_incident_in_the_past_id,
            merged_into_incident_id=self.merged_into_incident_id,
            merged_by=self.merged_by,
            merged_at=self.merged_at,
        )

        return db_incident


class SplitIncidentRequestDto(BaseModel):
    alert_fingerprints: list[str]
    destination_incident_id: UUID


class SplitIncidentResponseDto(BaseModel):
    destination_incident_id: UUID
    moved_alert_fingerprints: list[str]


class MergeIncidentsRequestDto(BaseModel):
    source_incident_ids: list[UUID]
    destination_incident_id: UUID


class MergeIncidentsResponseDto(BaseModel):
    merged_incident_ids: list[UUID]
    failed_incident_ids: list[UUID]
    destination_incident_id: UUID
    message: str


class IncidentSorting(Enum):
    creation_time = "creation_time"
    start_time = "start_time"
    last_seen_time = "last_seen_time"
    severity = "severity"
    status = "status"
    alerts_count = "alerts_count"

    creation_time_desc = "-creation_time"
    start_time_desc = "-start_time"
    last_seen_time_desc = "-last_seen_time"
    severity_desc = "-severity"
    status_desc = "-status"
    alerts_count_desc = "-alerts_count"

    def get_order_by(self, model):
        if self.value.startswith("-"):
            return desc(col(getattr(model, self.value[1:])))

        return col(getattr(model, self.value))


class IncidentListFilterParamsDto(BaseModel):
    statuses: List[IncidentStatus] = [s.value for s in IncidentStatus]
    severities: List[IncidentSeverity] = [s.value for s in IncidentSeverity]
    assignees: List[str]
    services: List[str]
    sources: List[str]


class IncidentCandidate(BaseModel):
    incident_name: str
    alerts: List[int] = Field(
        description="List of alert numbers (1-based index) included in this incident"
    )
    reasoning: str
    severity: str = Field(
        description="Assessed severity level",
        enum=["Low", "Medium", "High", "Critical"],
    )
    recommended_actions: List[str]
    confidence_score: float = Field(
        description="Confidence score of the incident clustering (0.0 to 1.0)"
    )
    confidence_explanation: str = Field(
        description="Explanation of how the confidence score was calculated"
    )


class IncidentClustering(BaseModel):
    incidents: List[IncidentCandidate]


class IncidentCommit(BaseModel):
    accepted: bool
    original_suggestion: dict
    changes: dict = Field(default_factory=dict)
    incident: IncidentDto


class IncidentsClusteringSuggestion(BaseModel):
    incident_suggestion: list[IncidentDto]
    suggestion_id: str

