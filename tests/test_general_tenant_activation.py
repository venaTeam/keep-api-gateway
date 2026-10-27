"""VENA-5596 -- activating the GENERAL tenant from the tenant switcher.

The UI sends the active tenant as a `keepActiveTenant=<id>&` prefix on the
bearer token. The GENERAL tenant is not backed by a Keycloak org group and
usually carries no `tenant_role_grant` row, so before this fix activating it
explicitly 401'd every caller who was not a superadmin -- even though the same
caller lands in that tenant implicitly when no org group applies.

Drives `_verify_bearer_token` with a mocked `decode_token` and a pre-seeded
tenant cache, so no live Keycloak and no DB are needed.
"""

import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services.identity_manager.identity_managers.keycloak import (
    keycloak_authverifier as kav,
)
from src.services.identity_manager.identity_managers.keycloak.keycloak_authverifier import (
    KeycloakAuthVerifier,
)
from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.services.identity_manager.rbac import Roles

ORG_A_TENANT = "11111111-1111-1111-1111-111111111111"
UNRELATED_TENANT = "33333333-3333-3333-3333-333333333333"
RAW_JWT = "eyJhbGciOiJSUzI1NiJ9.payload.sig"
ORG_GROUPS = ["/keep-org-a-admin"]


@pytest.fixture
def no_grants(monkeypatch):
    """Nobody holds a `tenant_role_grant` row."""
    monkeypatch.setattr(kav, "get_tenant_role_for_subjects", lambda *a, **k: None)


def _make_verifier(payload, superadmin_users=()):
    v = KeycloakAuthVerifier.__new__(KeycloakAuthVerifier)
    v.logger = logging.getLogger("test-verifier")
    v.roles_from_groups = True
    v.groups_claims = "groups"
    v.groups_org_prefix = "keep"
    v.groups_separator = "-"
    v.groups_claims_admin = "admin"
    v.groups_claims_noc = "noc"
    v.groups_claims_webhook = "webhook"
    v.keycloak_client_id = "keep"
    v.keycloak_roles = {
        "admin": Roles.ADMIN,
        "noc": Roles.NOC,
        "webhook": Roles.WEBHOOK,
    }
    v.superadmin_users = {u.lower() for u in superadmin_users}
    v.superadmin_groups = set()
    v._tenants = {"keep-org-a": {"tenant_id": ORG_A_TENANT, "tenant_logo_url": None}}
    v.keycloak_client = SimpleNamespace(
        decode_token=lambda token, validate=True: payload
    )
    return v


def _payload(email, groups, client_roles=None, keep_role=None):
    return {
        "keep_tenant_id": None,
        "preferred_username": email,
        "active_organization": {},
        "groups": groups,
        "resource_access": {"keep": {"roles": client_roles or []}},
        "keep_role": keep_role,
    }


def test_general_tenant_activation_resolves_instead_of_401(no_grants):
    """The reported bug: an org-group user activating GENERAL must land there."""
    v = _make_verifier(_payload("alice@keep.dev", ORG_GROUPS))
    entity = v._verify_bearer_token(f"keepActiveTenant={GENERIC_TENANT_UUID}&{RAW_JWT}")
    assert entity.tenant_id == GENERIC_TENANT_UUID
    assert entity.role == "editor"


def test_general_tenant_activation_uses_client_role_when_present(no_grants):
    v = _make_verifier(_payload("alice@keep.dev", ORG_GROUPS, client_roles=["noc"]))
    entity = v._verify_bearer_token(f"keepActiveTenant={GENERIC_TENANT_UUID}&{RAW_JWT}")
    assert entity.tenant_id == GENERIC_TENANT_UUID
    assert entity.role == "noc"


def test_general_tenant_activation_prefers_an_explicit_grant(monkeypatch):
    monkeypatch.setattr(kav, "get_tenant_role_for_subjects", lambda *a, **k: "viewer")
    v = _make_verifier(_payload("alice@keep.dev", ORG_GROUPS, client_roles=["admin"]))
    entity = v._verify_bearer_token(f"keepActiveTenant={GENERIC_TENANT_UUID}&{RAW_JWT}")
    assert entity.tenant_id == GENERIC_TENANT_UUID
    assert entity.role == "viewer"


def test_activating_an_unrelated_tenant_without_a_grant_still_401s(no_grants):
    """Guard: the GENERAL fallback must not widen access to arbitrary tenants."""
    v = _make_verifier(_payload("alice@keep.dev", ORG_GROUPS))
    with pytest.raises(HTTPException) as exc:
        v._verify_bearer_token(f"keepActiveTenant={UNRELATED_TENANT}&{RAW_JWT}")
    assert exc.value.status_code == 401


def test_org_group_activation_is_unchanged(no_grants):
    """Guard: a group-backed org still derives its role from the group."""
    v = _make_verifier(_payload("alice@keep.dev", ORG_GROUPS))
    entity = v._verify_bearer_token(f"keepActiveTenant={ORG_A_TENANT}&{RAW_JWT}")
    assert entity.tenant_id == ORG_A_TENANT
    assert entity.role == Roles.ADMIN.value
