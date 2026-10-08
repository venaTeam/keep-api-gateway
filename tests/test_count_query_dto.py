import pytest
from pydantic import ValidationError

from src.models.query import CountGroupBy, CountIncidentStatus, CountQueryDto, QueryDto


def test_ungrouped_query_has_no_group_by_or_status():
    query = CountQueryDto(cel="severity == 'critical'")

    assert query.group_by is None
    assert query.incident_status is None


def test_incident_group_defaults_to_active_status():
    query = CountQueryDto(group_by="incident")

    assert query.group_by == CountGroupBy.INCIDENT
    assert query.incident_status == CountIncidentStatus.ACTIVE


@pytest.mark.parametrize("status", ["active", "firing", "acknowledged"])
def test_incident_group_accepts_each_status(status):
    query = CountQueryDto(group_by="incident", incident_status=status)

    assert query.incident_status == CountIncidentStatus(status)


@pytest.mark.parametrize(
    "group_by",
    ["name", "service", "node_name", "application", "site", "assignee"],
)
def test_allowlisted_fields_are_accepted(group_by):
    query = CountQueryDto(group_by=group_by)

    assert query.group_by == CountGroupBy(group_by)
    assert query.incident_status is None


@pytest.mark.parametrize(
    "group_by",
    ["severity", "status", "environment", "source", "labels.team", "", "INCIDENT"],
)
def test_fields_outside_the_allowlist_are_rejected(group_by):
    with pytest.raises(ValidationError):
        CountQueryDto(group_by=group_by)


def test_incident_status_without_a_group_is_rejected():
    with pytest.raises(ValidationError):
        CountQueryDto(incident_status="firing")


def test_incident_status_with_a_non_incident_group_is_rejected():
    with pytest.raises(ValidationError):
        CountQueryDto(group_by="service", incident_status="active")


def test_unknown_incident_status_is_rejected():
    with pytest.raises(ValidationError):
        CountQueryDto(group_by="incident", incident_status="resolved")


def test_plain_query_dto_does_not_gain_the_new_fields():
    assert issubclass(CountQueryDto, QueryDto)
    assert "group_by" not in QueryDto.__fields__
    assert "incident_status" not in QueryDto.__fields__
