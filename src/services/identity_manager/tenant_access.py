"""Per-tenant authorization (VENA-5596).

Tenant/operator management is gated on the caller's role *in a specific tenant*,
not on a single global role. A superadmin is treated as admin of every tenant;
everyone else is resolved from `tenant_role_grant` by matching the caller's
subjects (email + Keycloak group paths) against the stored grants.

This is an endpoint-level check layered on top of the normal auth verifier -- it
is NOT the global `_authorize` bypass removal, which stays a separate story.
"""

import logging

from fastapi import Depends, HTTPException

from src.repositories import db
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.services.identity_manager.rbac import Roles

logger = logging.getLogger(__name__)

_ROLE_STRENGTH = {"admin": 3, "editor": 2, "viewer": 1}


def get_groups(entity: AuthenticatedEntity) -> list[str]:
    """The caller's Keycloak group paths, as carried on the token (empty for auth
    modes that don't populate groups)."""
    return getattr(entity, "groups", None) or []


def get_subjects(entity: AuthenticatedEntity) -> list[str]:
    """The identifiers to match against `tenant_role_grant.subject`: the caller's
    email plus their Keycloak group paths."""
    subjects: list[str] = []
    if getattr(entity, "email", None):
        subjects.append(entity.email)
    subjects.extend(get_groups(entity))
    return subjects


def is_superadmin(entity: AuthenticatedEntity) -> bool:
    return getattr(entity, "role", None) == Roles.SUPERADMIN.value


def resolve_tenant_role(entity: AuthenticatedEntity, tenant_id: str) -> str | None:
    """The caller's effective role on `tenant_id`: superadmin -> 'admin' on any
    tenant; otherwise the strongest grant they hold, or None."""
    if is_superadmin(entity):
        return "admin"
    return db.get_tenant_role_for_subjects(tenant_id, get_subjects(entity))


def assert_tenant_role(
    entity: AuthenticatedEntity, tenant_id: str, minimum: str = "admin"
) -> str:
    """Raise 403 unless the caller holds at least `minimum` on `tenant_id`.
    Use inside handlers where the tenant id is not a path param (e.g. operators,
    whose tenant comes from the operator row or request body)."""
    role = resolve_tenant_role(entity, tenant_id)
    if role is None or _ROLE_STRENGTH[role] < _ROLE_STRENGTH[minimum]:
        raise HTTPException(
            status_code=403, detail=f"Requires {minimum} role on this tenant"
        )
    return role


def _require(minimum: str):
    """Build a FastAPI dependency that enforces `minimum` on the path
    `{tenant_id}`. Use only on routes whose path contains `{tenant_id}`."""

    def dependency(
        tenant_id: str,
        authenticated_entity: AuthenticatedEntity = Depends(
            IdentityManagerFactory.get_auth_verifier(["read:tenants"])
        ),
    ) -> AuthenticatedEntity:
        assert_tenant_role(authenticated_entity, tenant_id, minimum)
        return authenticated_entity

    return dependency


require_tenant_admin = _require("admin")
require_tenant_member = _require("viewer")
