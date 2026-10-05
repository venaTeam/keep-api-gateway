"""Incident `status` folds mixed-case CEL literals on both of its sources.

`status` is mapped to two sources — the suppression derived from the dismiss
columns and the `incident.status` column — coalesced in that order. The enum
canonicalization happens on the queried literal rather than on the column, so
both sources must resolve a capitalized literal to the same canonical member.

The `incidentenrichment.enrichments` JSON is not a source: a `status` key stored
there must not shadow the incident's real status.
"""

from unittest.mock import patch

import pytest

from src.models.db.alert import IncidentEnrichment
from src.models.db.helpers import DismissMode
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
def incidents_with_mixed_status_sources(db_session):
    """Four incidents: one firing, one firing carrying a stale `status` enrichment
    key, one resolved, and one firing but dismissed."""
    _make_incident(db_session, "column-firing", IncidentStatus.FIRING)
    stale_key = _make_incident(db_session, "stale-enrichment", IncidentStatus.FIRING)
    _make_incident(db_session, "column-resolved", IncidentStatus.RESOLVED)
    dismissed = _make_incident(db_session, "dismissed", IncidentStatus.FIRING)
    dismissed.dismiss_mode = DismissMode.PERMANENT.value

    db_session.add(
        IncidentEnrichment(
            tenant_id=SINGLE_TENANT_UUID,
            incident_id=stale_key.id,
            enrichments={"status": IncidentStatus.RESOLVED.value},
        )
    )
    db_session.commit()


@pytest.mark.parametrize(
    "cel_query, expected_count",
    [
        ("status == 'firing'", 2),
        ("status == 'Firing'", 2),
        ("status == 'FIRING'", 2),
        ("status == 'resolved'", 1),
        ("status == 'Resolved'", 1),
        ("status == 'suppressed'", 1),
        ("status == 'Suppressed'", 1),
        ("status != 'Firing'", 2),
        ("status in ['Firing', 'Resolved']", 3),
    ],
)
def test_incident_status_casing_across_both_sources(
    db_session, incidents_with_mixed_status_sources, cel_query, expected_count
):
    with patch("src.repositories.incidents.engine", db_session.get_bind()):
        _, total_count = get_last_incidents_by_cel(
            tenant_id=SINGLE_TENANT_UUID, cel=cel_query
        )

    assert total_count == expected_count
