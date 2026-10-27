from enum import Enum
from typing import Optional

from pydantic import BaseModel, root_validator


class SortOptionsDto(BaseModel):
    sort_by: Optional[str]
    sort_dir: Optional[str]


class QueryDto(BaseModel):
    cel: Optional[str] = ""
    limit: Optional[int] = 1000
    offset: Optional[int] = 0
    sort_by: Optional[str]  # must be deprecated because we have sort_options
    sort_dir: Optional[str]  # must be deprecated because we have sort_options
    sort_options: Optional[list[SortOptionsDto]]


class CountGroupBy(str, Enum):
    """Fields a count may take distinct values of."""

    INCIDENT = "incident"
    NAME = "name"
    SERVICE = "service"
    NODE_NAME = "node_name"
    APPLICATION = "application"
    SITE = "site"
    ASSIGNEE = "assignee"


class CountIncidentStatus(str, Enum):
    """Which incidents count as live when grouping by incident."""

    ACTIVE = "active"
    FIRING = "firing"
    ACKNOWLEDGED = "acknowledged"


class CountQueryDto(QueryDto):
    """Body of ``POST /alerts/query/count``.

    Without ``group_by`` it counts alerts. With ``group_by`` it counts distinct
    values of that field; ``incident_status`` applies only to ``group_by=incident``
    and defaults to ``active`` there.
    """

    group_by: Optional[CountGroupBy] = None
    incident_status: Optional[CountIncidentStatus] = None

    @root_validator(skip_on_failure=True)
    def validate_incident_status(cls, values):
        group_by = values.get("group_by")
        incident_status = values.get("incident_status")

        if incident_status is not None and group_by != CountGroupBy.INCIDENT:
            raise ValueError("incident_status is only valid with group_by=incident")

        if group_by == CountGroupBy.INCIDENT and incident_status is None:
            values["incident_status"] = CountIncidentStatus.ACTIVE

        return values
