from datetime import datetime, timedelta

import pytest

from src.models.db.helpers import DismissMode
from src.models.db.incident import Incident, IncidentStatus
from src.models.query import CountQueryDto
from src.repositories.alerts import query_distinct_alerts_count
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.alert_graph import link, seed_alert, seed_incident


def count(**query):
    return query_distinct_alerts_count(
        tenant_id=SINGLE_TENANT_UUID, query=CountQueryDto(**query)
    )


def dismiss(session, incident, mode, until=None):
    incident.dismiss_mode = mode.value
    incident.dismissed_until = until
    session.commit()


@pytest.fixture
def graph(db_session):
    """Six linked incidents, four of them with a dismissal.

    firing, acknowledged:             counted with their stored status
    permanent dismissal:              suppressed, so not live
    acknowledged and dismissed:       suppressed, so not live
    dismissal until a future time:    suppressed, so not live
    dismissal whose deadline passed:  counted again with its stored status
    """
    now = datetime.utcnow()
    incidents = {
        "firing": seed_incident(db_session, "firing"),
        "acknowledged": seed_incident(
            db_session, "acknowledged", IncidentStatus.ACKNOWLEDGED
        ),
        "acknowledged-dismissed": seed_incident(
            db_session, "acknowledged-dismissed", IncidentStatus.ACKNOWLEDGED
        ),
        "permanent": seed_incident(db_session, "permanent"),
        "until-future": seed_incident(db_session, "until-future"),
        "until-expired": seed_incident(db_session, "until-expired"),
    }
    dismiss(db_session, incidents["permanent"], DismissMode.PERMANENT)
    dismiss(db_session, incidents["acknowledged-dismissed"], DismissMode.PERMANENT)
    dismiss(
        db_session,
        incidents["until-future"],
        DismissMode.DISMISS_UNTIL,
        now + timedelta(hours=2),
    )
    dismiss(
        db_session,
        incidents["until-expired"],
        DismissMode.DISMISS_UNTIL,
        now - timedelta(hours=2),
    )
    for index, (name, incident) in enumerate(incidents.items()):
        seed_alert(db_session, f"fp-{name}", name=f"alert-{name}", service=f"s{index}")
        link(db_session, f"fp-{name}", incident)


@pytest.mark.parametrize(
    "incident_status, expected",
    [(None, 3), ("active", 3), ("firing", 2), ("acknowledged", 1)],
)
def test_dismissed_incidents_are_not_counted_as_live(graph, incident_status, expected):
    """A dismissal in force hides the incident from every live count."""
    query = {"group_by": "incident"}
    if incident_status:
        query["incident_status"] = incident_status

    assert count(**query) == expected


def test_an_expired_dismissal_counts_again_without_any_write(graph, db_session):
    """The deadline is read at query time, so nothing has to rewrite the row."""
    expired = (
        db_session.query(Incident)
        .filter(Incident.user_generated_name == "until-expired")
        .one()
    )

    assert expired.status == IncidentStatus.FIRING.value
    assert count(group_by="incident", incident_status="firing") == 2


def test_alert_field_counts_ignore_incident_dismissal(graph):
    """Counting distinct alert values does not depend on any incident state."""
    assert count(group_by="service") == 6
