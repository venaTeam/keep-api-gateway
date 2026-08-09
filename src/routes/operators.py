import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.exceptions.tenant_exceptions import OperatorGroupTaken, OperatorNameTaken
from src.models.operator import CreateOperatorRequest, OperatorOut
from src.repositories import db
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.services.identity_manager.tenant_access import (
    assert_tenant_role,
    get_groups,
    is_superadmin,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# --- endpoints ------------------------------------------------------------


@router.post("", status_code=201, description="Create an operator (tenant admin)")
def create_operator_endpoint(
    body: CreateOperatorRequest,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:tenants"])
    ),
) -> OperatorOut:
    # Only an admin of the target tenant (or a superadmin) may add an operator.
    assert_tenant_role(authenticated_entity, body.tenant_id, "admin")
    # A non-superadmin may only claim one of their own Keycloak groups.
    if not is_superadmin(authenticated_entity) and body.group not in get_groups(
        authenticated_entity
    ):
        raise HTTPException(
            status_code=400, detail="group must be one of your groups"
        )
    try:
        operator = db.create_operator(
            group=body.group, tenant_id=body.tenant_id, name=body.name
        )
    except OperatorNameTaken:
        raise HTTPException(
            status_code=409, detail="operator name already exists"
        )
    except OperatorGroupTaken:
        raise HTTPException(
            status_code=409, detail="group already has an operator"
        )
    return operator


@router.get("", description="List operators for a tenant")
def list_operators_endpoint(
    tenant_id: Optional[str] = Query(default=None),
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:tenants"])
    ),
) -> List[OperatorOut]:
    if tenant_id is not None:
        assert_tenant_role(authenticated_entity, tenant_id, "viewer")
        return db.get_operators(tenant_id)
    if not is_superadmin(authenticated_entity):
        raise HTTPException(status_code=400, detail="tenant_id is required")
    return db.get_operators()


@router.get(
    "/available-groups", description="Groups that have no operator yet"
)
def available_groups_endpoint(
    search: Optional[str] = Query(default=None),
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:tenants"])
    ),
) -> List[str]:
    # A superadmin may create an operator for ANY group, so search Keycloak
    # SERVER-SIDE (fetching every group with member counts times out at scale).
    # A regular member is limited to their own token groups (a small list), so no
    # search is needed there -- matching the POST /operators group-ownership check.
    if is_superadmin(authenticated_entity):
        if not search:
            return []  # the async picker always sends ?search=; require it here
        identity_manager = IdentityManagerFactory.get_identity_manager(
            authenticated_entity.tenant_id
        )
        candidate = [
            (g.path or f"/{g.name}")
            for g in identity_manager.get_groups(search=search)
        ]
    else:
        candidate = get_groups(authenticated_entity)
        if search:
            s = search.lower()
            candidate = [g for g in candidate if s in g.lower()]

    in_use = db.operator_groups_in_use()
    return sorted({g for g in candidate if g and g not in in_use})


@router.get("/{operator_id}", description="Read one operator")
def get_operator_endpoint(
    operator_id: str,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:tenants"])
    ),
) -> OperatorOut:
    operator = db.get_operator(operator_id)
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    assert_tenant_role(authenticated_entity, operator.tenant_id, "viewer")
    return operator
