"""CEL string literals compared against a property that declares ``enum_values``
must be resolved to their canonical stored form case-insensitively.

The facets panel renders enum values capitalized ("Firing", "Critical") while the
database stores them lowercase, so users routinely type the capitalized form into
the CEL box. These tests pin the generated SQL for mixed-case literals against the
real, shipped alert and incident property metadata, in every SQL dialect.
"""

import pytest

from src.models.db.incident import IncidentStatus
from src.repositories.alerts import properties_metadata as alert_properties_metadata
from src.repositories.cel_to_sql.properties_metadata import (
    JsonFieldMapping,
    SimpleFieldMapping,
)
from src.repositories.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
    get_cel_to_sql_provider_for_dialect,
)
from src.repositories.incidents import (
    properties_metadata as incident_properties_metadata,
)

DIALECTS = ["sqlite", "mysql", "postgresql"]

ALERT_STATUS_COLUMN = "COALESCE(lastalert.status, alert.status)"
INCIDENT_STATUS_COLUMN = {
    "sqlite": (
        "CAST(COALESCE(json_extract(incidentenrichment.enrichments, '$.\"status\"'), "
        "CAST(incident.status AS TEXT)) as TEXT)"
    ),
    "mysql": (
        "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(incidentenrichment.enrichments, "
        "'$.\"status\"')), CAST(incident.status AS TEXT))"
    ),
    "postgresql": (
        "(COALESCE((incidentenrichment.enrichments) ->> 'status', "
        "CAST(incident.status AS TEXT)))::TEXT"
    ),
}


def _all_dialects(expected_sql: str) -> dict:
    return {dialect: expected_sql for dialect in DIALECTS}


def _incident_status(suffix: str) -> dict:
    return {
        dialect: f"{INCIDENT_STATUS_COLUMN[dialect]} {suffix}" for dialect in DIALECTS
    }


ALERT_CASES = {
    "eq titlecase status": (
        "status == 'Firing'",
        _all_dialects(f"{ALERT_STATUS_COLUMN} = 'firing'"),
    ),
    "eq uppercase status": (
        "status == 'FIRING'",
        _all_dialects(f"{ALERT_STATUS_COLUMN} = 'firing'"),
    ),
    "eq lowercase status is unchanged": (
        "status == 'firing'",
        _all_dialects(f"{ALERT_STATUS_COLUMN} = 'firing'"),
    ),
    "ne titlecase status": (
        "status != 'Firing'",
        _all_dialects(f"{ALERT_STATUS_COLUMN} != 'firing'"),
    ),
    "in list folds every element": (
        "status in ['Firing', 'RESOLVED']",
        _all_dialects(f"{ALERT_STATUS_COLUMN} in ('firing', 'resolved')"),
    ),
    "eq titlecase severity": (
        "severity == 'Critical'",
        _all_dialects("alert.severity = 'critical'"),
    ),
    "eq uppercase severity": (
        "severity == 'CRITICAL'",
        _all_dialects("alert.severity = 'critical'"),
    ),
    "gt highest severity matches nothing": (
        "severity > 'Critical'",
        {"sqlite": "false", "mysql": "FALSE", "postgresql": "false"},
    ),
    "ge severity excludes lower ranks": (
        "severity >= 'Info'",
        _all_dialects("alert.severity in ('info', 'warning', 'high', 'critical')"),
    ),
    "lt severity": (
        "severity < 'Critical'",
        _all_dialects("NOT (alert.severity in ('critical'))"),
    ),
    "le severity": (
        "severity <= 'High'",
        _all_dialects("NOT (alert.severity in ('critical'))"),
    ),
    "eq titlecase environment": (
        "environment == 'Production'",
        _all_dialects("alert.environment = 'production'"),
    ),
}

ALERT_UNCHANGED_CASES = {
    "free text name keeps its casing": (
        "name == 'Firing'",
        _all_dialects("alert.name = 'Firing'"),
    ),
    "free text assignee keeps its casing": (
        "assignee == 'Alice'",
        _all_dialects("lastalert.assignee = 'Alice'"),
    ),
    "free text source keeps its casing": (
        "source == 'Datadog'",
        _all_dialects("alert.provider_type = 'Datadog'"),
    ),
    "unknown enum literal passes through": (
        "status == 'flapping'",
        _all_dialects(f"{ALERT_STATUS_COLUMN} = 'flapping'"),
    ),
    "unknown ordering literal keeps the existing fallback": (
        "severity > 'Flapping'",
        _all_dialects(
            "alert.severity in ('low', 'info', 'warning', 'high', 'critical')"
        ),
    ),
    "contains is left to the dialect": (
        "severity.contains('Crit')",
        {
            "sqlite": "alert.severity IS NOT NULL AND alert.severity LIKE '%Crit%'",
            "mysql": (
                "alert.severity IS NOT NULL AND LOWER(alert.severity) LIKE '%crit%'"
            ),
            "postgresql": "alert.severity IS NOT NULL AND alert.severity ILIKE '%Crit%'",
        },
    ),
    "startsWith is left to the dialect": (
        "severity.startsWith('Crit')",
        {
            "sqlite": "alert.severity IS NOT NULL AND alert.severity LIKE 'Crit%'",
            "mysql": (
                "alert.severity IS NOT NULL AND LOWER(alert.severity) LIKE 'crit%'"
            ),
            "postgresql": "alert.severity IS NOT NULL AND alert.severity ILIKE 'Crit%'",
        },
    ),
}

INCIDENT_CASES = {
    "eq titlecase status": ("status == 'Firing'", _incident_status("= 'firing'")),
    "eq uppercase status": ("status == 'FIRING'", _incident_status("= 'firing'")),
    "ne titlecase status": ("status != 'Firing'", _incident_status("!= 'firing'")),
    "in list folds every element": (
        "status in ['Firing', 'RESOLVED']",
        _incident_status("in ('firing', 'resolved')"),
    ),
    "eq titlecase merged status": (
        "status == 'Merged'",
        _incident_status("= 'merged'"),
    ),
    "eq titlecase acknowledged status": (
        "status == 'Acknowledged'",
        _incident_status("= 'acknowledged'"),
    ),
    "eq titlecase deleted status": (
        "status == 'Deleted'",
        _incident_status("= 'deleted'"),
    ),
    "free text name keeps its casing": (
        "name == 'Firing'",
        _all_dialects(
            "COALESCE(incident.user_generated_name, incident.ai_generated_name) "
            "= 'Firing'"
        ),
    ),
    "unknown enum literal passes through": (
        "status == 'flapping'",
        _incident_status("= 'flapping'"),
    ),
}


def _to_sql(dialect: str, metadata, cel: str) -> str:
    return get_cel_to_sql_provider_for_dialect(dialect, metadata).convert_to_sql_str(
        cel
    )


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_name", list(ALERT_CASES.keys()))
def test_alert_enum_literal_is_canonicalized(case_name, dialect):
    cel, expected_by_dialect = ALERT_CASES[case_name]
    assert _to_sql(dialect, alert_properties_metadata, cel) == expected_by_dialect[
        dialect
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_name", list(ALERT_UNCHANGED_CASES.keys()))
def test_alert_non_enum_literal_is_untouched(case_name, dialect):
    cel, expected_by_dialect = ALERT_UNCHANGED_CASES[case_name]
    assert _to_sql(dialect, alert_properties_metadata, cel) == expected_by_dialect[
        dialect
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case_name", list(INCIDENT_CASES.keys()))
def test_incident_status_enum_literal_is_canonicalized(case_name, dialect):
    cel, expected_by_dialect = INCIDENT_CASES[case_name]
    assert _to_sql(dialect, incident_properties_metadata, cel) == expected_by_dialect[
        dialect
    ]


def test_incident_status_declares_the_full_canonical_value_set():
    """The enrichment-override source and the column source share one enum set."""
    status_metadata = incident_properties_metadata.get_property_metadata(["status"])

    assert status_metadata.enum_values is not None
    assert sorted(status_metadata.enum_values) == sorted(
        [status.value for status in IncidentStatus]
    )
    assert len(status_metadata.field_mappings) == 2
    assert isinstance(status_metadata.field_mappings[0], JsonFieldMapping)
    assert isinstance(status_metadata.field_mappings[1], SimpleFieldMapping)


def test_incident_status_facet_lists_firing_first():
    """Enum padding drives facet ordering; incidents must match the alerts feed."""
    status_metadata = incident_properties_metadata.get_property_metadata(["status"])
    alert_status_metadata = alert_properties_metadata.get_property_metadata(["status"])

    def facet_order(enum_values):
        return sorted(
            enum_values, key=lambda value: enum_values.index(value), reverse=True
        )

    assert facet_order(alert_status_metadata.enum_values)[0] == "firing"
    assert facet_order(status_metadata.enum_values)[0] == "firing"
