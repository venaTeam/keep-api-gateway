"""VENA-5596 -- superadmin resolution in the Keycloak verifier.

Drives `_verify_bearer_token` with a mocked `decode_token`, so the superadmin
allowlist path is covered without a live Keycloak. The verifier is built bare
(via __new__) to skip __init__'s network calls; only the attributes the token
path reads are set. The email-based, no-org-group path touches neither the DB
nor Keycloak, so no db_session is needed.
"""

import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services.identity_manager.identity_managers.keycloak.keycloak_authverifier import (
    KeycloakAuthVerifier,
)
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.services.identity_manager.rbac import Roles


def _make_verifier(superadmin_users=(), superadmin_groups=(), payload=None):
    """A KeycloakAuthVerifier with only the attributes the token path reads."""
    v = KeycloakAuthVerifier.__new__(KeycloakAuthVerifier)
    v.logger = logging.getLogger("test-verifier")
    v.roles_from_groups = True
    v.groups_claims = "groups"
    v.groups_org_prefix = "keep"
    v.groups_separator = "-"
    v.groups_claims_admin = "admin"
    v.groups_claims_noc = "noc"
    v.groups_claims_webhook = "webhook"
    v.keycloak_roles = {
        "admin": Roles.ADMIN,
        "noc": Roles.NOC,
        "webhook": Roles.WEBHOOK,
    }
    v.superadmin_users = {u.lower() for u in superadmin_users}
    v.superadmin_groups = {g.lower() for g in superadmin_groups}
    v.keycloak_client = SimpleNamespace(
        decode_token=lambda token, validate=True: payload
    )
    return v


def _payload(email, groups):
    return {
        "keep_tenant_id": None,
        "preferred_username": email,
        "active_organization": {},
        "groups": groups,
    }


# --- _is_superadmin (the allowlist helper) --------------------------------


def test_is_superadmin_by_email_case_insensitive():
    v = _make_verifier(superadmin_users={"alice@keep.dev"})
    assert v._is_superadmin("alice@keep.dev", []) is True
    assert v._is_superadmin("ALICE@keep.dev", []) is True
    assert v._is_superadmin("bob@keep.dev", []) is False


def test_is_superadmin_by_group_case_insensitive():
    v = _make_verifier(superadmin_groups={"/superadmins"})
    assert v._is_superadmin("bob@keep.dev", ["/superadmins"]) is True
    assert v._is_superadmin("bob@keep.dev", ["/SUPERADMINS"]) is True
    assert v._is_superadmin("bob@keep.dev", ["/keep-org-a-admin"]) is False


def test_is_superadmin_empty_allowlist_is_nobody():
    v = _make_verifier()
    assert v._is_superadmin("alice@keep.dev", ["/superadmins"]) is False


# --- _verify_bearer_token (end-to-end role resolution) --------------------


def test_verify_token_superadmin_by_email_no_org_group():
    payload = _payload("alice@keep.dev", [])
    v = _make_verifier(superadmin_users={"alice@keep.dev"}, payload=payload)
    entity = v._verify_bearer_token("sometoken")
    assert entity.role == "superadmin"
    assert entity.email == "alice@keep.dev"
    assert entity.groups == []
    # tenant-less superadmin falls back to the default tenant as active context
    assert entity.tenant_id == SINGLE_TENANT_UUID


def test_verify_token_superadmin_by_group_no_org_group():
    # a non-org group (does not start with the org prefix) that grants superadmin
    payload = _payload("bob@keep.dev", ["/superadmins"])
    v = _make_verifier(superadmin_groups={"/superadmins"}, payload=payload)
    entity = v._verify_bearer_token("sometoken")
    assert entity.role == "superadmin"
    assert entity.groups == ["/superadmins"]


def test_verify_token_non_superadmin_no_org_group_401():
    # not on any allowlist and in no org group -> misconfigured token, 401
    payload = _payload("bob@keep.dev", [])
    v = _make_verifier(payload=payload)
    with pytest.raises(HTTPException) as exc:
        v._verify_bearer_token("sometoken")
    assert exc.value.status_code == 401
