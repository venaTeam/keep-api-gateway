"""Incident `status` folds mixed-case CEL literals on both of its sources.

`status` is mapped to two sources — the `incidentenrichment.enrichments` JSON
override and the `incident.status` column — coalesced in that order. The enum
canonicalization happens on the queried literal rather than on the column, so
both sources must resolve a capitalized literal to the same canonical member.
"""

from unittest.mock import patch

import pytest

from src.models.db.alert import IncidentEnrichment
from src.models.db.incident import Incident, IncidentStatus
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.repositories.incidents import get_last_incidents_by_cel


def _make_incident(db_session, name: str, status: IncidentStatus) -> Incident:
    incident = Incident(
        tenant_id=SINGLE_TENANT_UUID,
        user_generated_name=name,
        user_summary=name,
        generated_summary=name,
        status=status.value,
    )
    db_session.add(incident)
    db_session.commit()
    return incident


@pytest.fixture
def incidents_with_status_override(db_session):
    """Three incidents: one firing, one firing overridden to resolved, one resolved."""
    _make_incident(db_session, "column-firing", IncidentStatus.FIRING)
    overridden = _make_incident(db_session, "override-me", IncidentStatus.FIRING)
    _make_incident(db_session, "column-resolved", IncidentStatus.RESOLVED)

    db_session.add(
        IncidentEnrichment(
            tenant_id=SINGLE_TENANT_UUID,
            incident_id=overridden.id,
            enrichments={"status": IncidentStatus.RESOLVED.value},
        )
    )
    db_session.commit()


@pytest.mark.parametrize(
    "cel_query, expected_count",
    [
        ("status == 'firing'", 1),
        ("status == 'Firing'", 1),
        ("status == 'FIRING'", 1),
        ("status == 'resolved'", 2),
        ("status == 'Resolved'", 2),
        ("status != 'Firing'", 2),
        ("status in ['Firing', 'Resolved']", 3),
    ],
)
def test_incident_status_casing_across_both_sources(
    db_session, incidents_with_status_override, cel_query, expected_count
):
    with patch("src.repositories.incidents.engine", db_session.get_bind()):
        _, total_count = get_last_incidents_by_cel(
            tenant_id=SINGLE_TENANT_UUID, cel=cel_query
        )

    assert total_count == expected_count
