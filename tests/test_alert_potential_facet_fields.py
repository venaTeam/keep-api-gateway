from src.models.alert import AlertDto
from src.models.db.alert import AlertField
from src.models.db.tenant import Tenant
from src.repositories.alerts import get_alert_potential_facet_fields
from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.repositories.incidents import get_incident_potential_facet_fields

OTHER_TENANT_ID = "other-tenant"
DECLARED_SAMPLE = {"name", "status", "severity", "description", "source"}


def _add_fields(db_session, tenant_id, field_names):
    for field_name in field_names:
        db_session.add(
            AlertField(
                tenant_id=tenant_id,
                field_name=field_name,
                provider_id=None,
                provider_type=None,
            )
        )
    db_session.commit()


def test_tenant_without_observed_fields_gets_declared_alert_fields(db_session):
    """A tenant with no alertfield rows still gets the alert schema fields."""
    fields = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)

    assert DECLARED_SAMPLE <= set(fields)


def test_observed_fields_are_added_to_declared_fields(db_session):
    """Observed custom fields are returned next to the declared ones."""
    _add_fields(db_session, GENERIC_TENANT_UUID, ["labels.team", "service"])

    fields = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)

    assert {"labels.team", "service"} <= set(fields)
    assert DECLARED_SAMPLE <= set(fields)


def test_field_declared_and_observed_is_returned_once(db_session):
    """A field that is both declared and observed is not duplicated."""
    _add_fields(db_session, GENERIC_TENANT_UUID, ["name", "status"])

    fields = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)

    assert len(fields) == len(set(fields))
    assert fields.count("name") == 1


def test_observed_fields_of_other_tenants_are_not_returned(db_session):
    """Only the requested tenant's observed fields are mixed into the result."""
    db_session.add(
        Tenant(id=OTHER_TENANT_ID, name="other", created_by="tests@keephq.dev")
    )
    db_session.commit()
    _add_fields(db_session, OTHER_TENANT_ID, ["labels.secret"])

    fields = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)

    assert "labels.secret" not in fields


def test_incident_fields_include_every_declared_alert_field(db_session):
    """Incident facet fields offer each declared alert field, even for an empty tenant."""
    fields = set(get_incident_potential_facet_fields(GENERIC_TENANT_UUID))

    declared = {f"alert.{name}" for name in AlertDto.__fields__}
    assert declared <= fields
