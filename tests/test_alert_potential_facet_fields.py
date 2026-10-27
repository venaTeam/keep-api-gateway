import pytest

from src.models.db.alert import AlertField
from src.models.db.tenant import Tenant
from src.repositories.alerts import get_alert_potential_facet_fields
from src.repositories.alerts import properties_metadata as alert_properties_metadata
from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.repositories.incidents import get_incident_potential_facet_fields
from src.repositories.incidents import (
    properties_metadata as incident_properties_metadata,
)
from tests.fixtures.client import client, test_app  # noqa

OTHER_TENANT_ID = "other-tenant"
QUERYABLE_SAMPLE = {
    "name",
    "status",
    "severity",
    "description",
    "source",
    "providerId",
    "providerType",
}
NOT_QUERYABLE = {
    "component",
    "dismiss_until",
    "incident",
    "ticket_provider_id",
    "ticket_type",
    "ticket_url",
    "provider_id",
    "provider_type",
}


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


def _unresolved(properties_metadata, field_names):
    return [
        name
        for name in field_names
        if properties_metadata.get_property_metadata_for_str(name) is None
    ]


def test_tenant_without_observed_fields_gets_the_queryable_declared_fields(db_session):
    """A tenant with no alertfield rows still gets the declared fields the query layer accepts."""
    fields = set(get_alert_potential_facet_fields(GENERIC_TENANT_UUID))

    assert QUERYABLE_SAMPLE <= fields
    assert fields.isdisjoint(NOT_QUERYABLE)


def test_every_declared_field_offered_resolves_in_the_alert_metadata(db_session):
    """Every name offered to an empty tenant can be used for filtering and facets."""
    fields = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)

    assert _unresolved(alert_properties_metadata, fields) == []


def test_observed_fields_follow_the_declared_fields_in_sorted_order(db_session):
    """Observed fields come after the declared ones, sorted, so the list is stable."""
    declared = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)
    _add_fields(
        db_session, GENERIC_TENANT_UUID, ["labels.zeta", "custom_zz", "labels.alpha"]
    )

    fields = get_alert_potential_facet_fields(GENERIC_TENANT_UUID)

    assert fields[: len(declared)] == declared
    assert fields[len(declared) :] == ["custom_zz", "labels.alpha", "labels.zeta"]


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


def test_incident_fields_for_an_empty_tenant_all_resolve_in_the_incident_metadata(
    db_session,
):
    """Incident facet fields do not gain alert.* names the incident query layer rejects."""
    fields = get_incident_potential_facet_fields(GENERIC_TENANT_UUID)

    assert _unresolved(incident_properties_metadata, fields) == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_facet_fields_route_offers_only_queryable_fields_to_an_empty_tenant(
    db_session, client, test_app
):
    """GET /alerts/facets/fields returns usable names for a tenant without alertfield rows."""
    response = client.get(
        "/alerts/facets/fields", headers={"x-api-key": "some-api-key"}
    )

    assert response.status_code == 200
    fields = response.json()
    assert QUERYABLE_SAMPLE <= set(fields)
    assert _unresolved(alert_properties_metadata, fields) == []
