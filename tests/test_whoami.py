"""GET /whoami: who the caller is and what their role may do."""

import pytest

from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.rbac import get_role_by_role_name
from tests.fixtures.client import client, setup_api_key, test_app  # noqa
from tests.fixtures.identity import bearer_headers


@pytest.mark.parametrize("role", ["noc", "viewer", "editor", "admin", "superadmin"])
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_whoami_returns_email_and_role_scopes(db_session, client, test_app, role):
    response = client.get("/whoami", headers=bearer_headers(role))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "tenant_id": GENERIC_TENANT_UUID,
        "role": role,
        "email": f"{role}@example.com",
        "scopes": get_role_by_role_name(role).SCOPES,
    }


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_whoami_noc_scopes_are_spelled_out(db_session, client, test_app):
    response = client.get("/whoami", headers=bearer_headers("noc"))

    assert response.json()["scopes"] == ["read:*", "write:alert"]


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_whoami_api_key_reports_its_creator(db_session, client, test_app):
    setup_api_key(db_session, "some-key", role="admin")

    response = client.get("/whoami", headers={"x-api-key": "some-key"})

    body = response.json()
    assert body["email"] == "admin@keephq"
    assert body["role"] == "admin"
    assert body["scopes"] == get_role_by_role_name("admin").SCOPES


# Over HTTP an unknown role is refused before the handler runs, but a verifier that
# authorizes elsewhere (Keycloak UMA) can let one through, so call it directly.
@pytest.mark.parametrize("role", ["custom-keycloak-role", "", None])
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_whoami_scopes_are_null_for_a_role_outside_rbac(db_session, test_app, role):
    from src.routes.whoami import get_tenant_id

    entity = AuthenticatedEntity(
        tenant_id="some-tenant", email="u@example.com", role=role
    )

    assert get_tenant_id(entity) == {
        "tenant_id": "some-tenant",
        "role": role,
        "email": "u@example.com",
        "scopes": None,
    }


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_whoami_does_not_hand_out_the_role_definition(db_session, test_app):
    """The returned list is a copy: changing it must not change the role."""
    from src.routes.whoami import get_tenant_id

    entity = AuthenticatedEntity(tenant_id="t", email="u@example.com", role="noc")
    get_tenant_id(entity)["scopes"].append("delete:*")

    assert get_role_by_role_name("noc").SCOPES == ["read:*", "write:alert"]
