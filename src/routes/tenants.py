import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from src.exceptions.tenant_exceptions import TenantNameConflict, TenantNotFound
from src.models.tenant import (
    CreateTenantRequest,
    RoleAssignment,
    TenantOut,
    UpdateTenantRequest,
)
from src.repositories import db
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.services.identity_manager.tenant_access import (
    get_subjects,
    is_superadmin,
    require_tenant_admin,
    require_tenant_member,
)

router = APIRouter()
logger = logging.getLogger(__name__)

VALID_SUBJECT_TYPES = {"user", "group"}
VALID_ROLES = {"admin", "editor", "viewer"}


# --- validation helpers ---------------------------------------------------


def _validate_role_mappings(role_mappings: List[RoleAssignment]) -> None:
    for mapping in role_mappings:
        if mapping.subject_type not in VALID_SUBJECT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid subject_type '{mapping.subject_type}' (expected user/group)",
            )
        if mapping.role not in VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role '{mapping.role}' (expected admin/editor/viewer)",
            )
    subjects = [m.subject for m in role_mappings]
    if len(subjects) != len(set(subjects)):
        raise HTTPException(status_code=400, detail="Duplicate subject in role_mappings")


def _guard_self_modification(entity: AuthenticatedEntity, subject: str) -> None:
    """An admin cannot modify their own permissions (VENA-5596). A superadmin is
    exempt."""
    if not is_superadmin(entity) and subject in get_subjects(entity):
        raise HTTPException(
            status_code=400, detail="You cannot modify your own permissions"
        )


# --- endpoints ------------------------------------------------------------


@router.post("", status_code=201, description="Create a new tenant (superadmin only)")
def create_tenant_endpoint(
    body: CreateTenantRequest,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["create:tenant"])
    ),
) -> dict:
    # Explicit superadmin gate. The scope dependency alone is not enough under
    # Keycloak, where _authorize is bypassed -- so enforce it in-handler, the same
    # way the per-tenant endpoints gate with assert_tenant_role (VENA-5596).
    if not is_superadmin(authenticated_entity):
        raise HTTPException(
            status_code=403, detail="Only a superadmin can create a tenant"
        )
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Tenant name is required")
    _validate_role_mappings(body.role_mappings)
    if not any(m.role == "admin" for m in body.role_mappings):
        raise HTTPException(
            status_code=400, detail="At least one admin assignment is required"
        )
    try:
        tenant = db.create_tenant_atomic(
            name=body.name.strip(),
            role_mappings=body.role_mappings,
            created_by=authenticated_entity.email,
        )
    except TenantNameConflict:
        raise HTTPException(status_code=409, detail="Tenant name already exists")

    return {
        "id": tenant.id,
        "name": tenant.name,
        "role_mappings": [m.dict() for m in body.role_mappings],
    }


@router.get("", description="List tenants")
def list_tenants_endpoint(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:tenants"])
    ),
) -> List[TenantOut]:
    if is_superadmin(authenticated_entity):
        tenants = db.get_tenants()
    else:
        tenants = db.get_tenants_for_subjects(get_subjects(authenticated_entity))
    return tenants


@router.get("/{tenant_id}", description="Read one tenant (roles + operators)")
def get_tenant_endpoint(
    tenant_id: str,
    authenticated_entity: AuthenticatedEntity = Depends(require_tenant_member),
) -> dict:
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "id": tenant.id,
        "name": tenant.name,
        "roles": [
            {"subject": g.subject, "subject_type": g.subject_type, "role": g.role}
            for g in db.list_grants(tenant_id)
        ],
        "operators": [
            {"id": o.id, "name": o.name, "group": o.group}
            for o in db.get_operators(tenant_id)
        ],
    }


@router.put("/{tenant_id}", description="Edit a tenant (name)")
def update_tenant_endpoint(
    tenant_id: str,
    body: UpdateTenantRequest,
    authenticated_entity: AuthenticatedEntity = Depends(require_tenant_admin),
) -> TenantOut:
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Tenant name cannot be empty")
    try:
        tenant = db.update_tenant(tenant_id, name=body.name.strip())
    except TenantNotFound:
        raise HTTPException(status_code=404, detail="Tenant not found")
    except TenantNameConflict:
        raise HTTPException(status_code=409, detail="Tenant name already exists")
    return tenant


@router.post("/{tenant_id}/roles", description="Add or update a role grant")
def add_role_endpoint(
    tenant_id: str,
    body: RoleAssignment,
    authenticated_entity: AuthenticatedEntity = Depends(require_tenant_admin),
) -> dict:
    _validate_role_mappings([body])
    _guard_self_modification(authenticated_entity, body.subject)
    grant = db.add_grant(tenant_id, body.subject, body.subject_type, body.role)
    return {
        "subject": grant.subject,
        "subject_type": grant.subject_type,
        "role": grant.role,
    }


@router.delete("/{tenant_id}/roles/{subject:path}", description="Remove a role grant")
def remove_role_endpoint(
    tenant_id: str,
    subject: str,
    authenticated_entity: AuthenticatedEntity = Depends(require_tenant_admin),
) -> dict:
    _guard_self_modification(authenticated_entity, subject)
    removed = db.remove_grant(tenant_id, subject)
    if not removed:
        raise HTTPException(status_code=404, detail="Grant not found")
    return {"status": "OK"}
